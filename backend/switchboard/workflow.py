"""Step Functions callbacks advance only after durable business receipts exist."""

import json
import time
from datetime import datetime
from typing import Literal
from uuid import UUID

import boto3
from botocore.exceptions import ClientError

from switchboard.configuration import runtime_settings
from switchboard.dynamodb import DynamoStore, encode
from switchboard.models import Record, Text
from switchboard.storage import WorkspaceStorage

Stage = Literal["approval", "execution", "verification"]


class WorkflowInput(Record):
    workspace_id: Text
    generation: UUID
    run_id: UUID


def start(storage: WorkspaceStorage, run_id: str) -> bool:
    arn = runtime_settings().state_machine_arn
    if not arn:
        return False

    store = _database(storage)
    payload = WorkflowInput.model_validate_json(
        json.dumps(
            {
                "workspace_id": storage.workspace_id,
                "generation": store.generation(storage.workspace_id),
                "run_id": run_id,
            }
        )
    )
    try:
        boto3.client("stepfunctions").start_execution(
            stateMachineArn=arn,
            name=f"run-{payload.run_id}",
            input=payload.model_dump_json(),
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise
    return True


def _database(storage: WorkspaceStorage) -> DynamoStore:
    if not isinstance(storage.transport, DynamoStore):
        raise ValueError("Workflow callbacks require DynamoDB")

    return storage.transport


def _workspace(payload: WorkflowInput) -> WorkspaceStorage:
    storage = WorkspaceStorage.from_environment(workspace_id=payload.workspace_id)
    store = _database(storage)
    metadata = store.metadata(storage.workspace_id)
    if not metadata or metadata["generation"] != str(payload.generation):
        raise PermissionError("Workspace has changed")

    if (
        metadata["identityMode"] == "demo"
        and time.time() - datetime.fromisoformat(metadata["updatedAt"]).timestamp()
        > 86400
    ):
        raise PermissionError("Workspace has expired")

    store.generations[storage.workspace_id] = str(payload.generation)
    return storage


def summary(storage: WorkspaceStorage, run_id: str) -> dict:
    """Expose workflow metadata, keeping model output and callback tokens private."""
    store = _database(storage)
    job = store.get(store.partition(storage.workspace_id), f"JOB#{run_id}")
    if job and job["status"] in {"queued", "running"}:
        return {"investigate": True}

    if job and job["status"] == "failed":
        raise ValueError("Investigation failed")

    run = storage.get_run(run_id=run_id)
    if not run:
        raise ValueError("Investigation unavailable")

    proposal = run["result"]["proposal"]
    if proposal is None:
        return {"investigate": False, "outcome": "blocked"}

    saved = storage.get_proposal(proposal_id=proposal["id"])
    if not saved:
        raise ValueError("Proposal unavailable")

    return {
        "investigate": False,
        "outcome": "proposal_candidate",
        "proposal_id": saved["id"],
        "environment": saved["environment"],
    }


def _receipt(storage: WorkspaceStorage, run_id: str, stage: Stage) -> dict | None:
    resolved = summary(storage, run_id)
    proposal_id = resolved.get("proposal_id")
    if not proposal_id:
        return None

    if stage == "approval":
        return storage.get_approval(proposal_id=proposal_id)

    execution = storage.get_execution(proposal_id=proposal_id)
    if stage == "execution" or execution is None:
        return execution

    return storage.get_delivery_verification(execution_id=execution["id"])


def _callback(token: str, *, output: dict | None = None, error: str | None = None):
    client = boto3.client("stepfunctions")
    try:
        if error:
            client.send_task_failure(taskToken=token, error=error)
        else:
            client.send_task_success(taskToken=token, output=json.dumps(output))
    except ClientError as failure:
        if failure.response["Error"]["Code"] not in {
            "TaskDoesNotExist",
            "TaskTimedOut",
            "InvalidToken",
        }:
            raise


def complete_investigation(message: dict):
    token = message.get("task_token")
    if not token:
        return

    payload = WorkflowInput.model_validate_json(
        json.dumps({key: message[key] for key in WorkflowInput.model_fields})
    )
    try:
        storage = _workspace(payload)
        output = summary(storage, str(payload.run_id))
    except (PermissionError, ValueError):
        _callback(token, error="InvestigationUnavailable")
        return

    if not output["investigate"]:
        _callback(token, output=output)


def advance(storage: WorkspaceStorage, run_id: str, stage: Stage):
    """Replays and maintenance can deliver a callback lost after an action commit."""
    store = _database(storage)
    waiting = store.get(store.partition(storage.workspace_id), f"FLOW#{run_id}#{stage}")
    if not waiting:
        return

    receipt = _receipt(storage, run_id, stage)
    if receipt is None:
        return

    output = {"receipt_id": receipt["id"]}
    if stage == "verification":
        output["outcome"] = receipt["outcome"]
    _callback(waiting["token"], output=output)
    operation = store.delete(
        store.partition(storage.workspace_id), f"FLOW#{run_id}#{stage}"
    )
    operation["Delete"].update(
        ConditionExpression="#body = :waiting",
        ExpressionAttributeNames={"#body": "body"},
        ExpressionAttributeValues={":waiting": {"S": encode(waiting)}},
    )
    store.transaction(
        storage.workspace_id,
        [operation],
    )


def signal(storage: WorkspaceStorage, run_id: UUID, stage: Stage):
    if start(storage, str(run_id)):
        advance(storage, str(run_id), stage)


def handle(event: dict):
    """Only direct IAM invocations may register callback tokens."""
    payload = WorkflowInput.model_validate_json(json.dumps(event["workflow"]))
    storage = _workspace(payload)
    if event["operation"] == "resolve":
        return summary(storage, str(payload.run_id))

    if event["operation"] != "wait" or event["stage"] not in {
        "approval",
        "execution",
        "verification",
    }:
        raise ValueError("Unsupported workflow operation")

    # 1. Resolve the saved proposal before recording a private callback capability.
    stage: Stage = event["stage"]
    resolved = summary(storage, str(payload.run_id))
    if not resolved.get("proposal_id"):
        raise ValueError("Workflow has no proposal")

    store = _database(storage)
    partition = store.partition(storage.workspace_id)
    sort = f"FLOW#{payload.run_id}#{stage}"
    record = {
        "token": event["task_token"],
        "run_id": str(payload.run_id),
        "stage": stage,
    }
    previous = store.get(partition, sort)
    operation = (
        store.replace(partition, sort, previous, record)
        if previous
        else store.put(partition, sort, record, absent=True)
    )
    store.transaction(storage.workspace_id, [operation])

    # 2. An action committed before token registration must still advance the wait.
    advance(storage, str(payload.run_id), stage)
    return {"waiting": stage}
