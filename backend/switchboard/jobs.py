"""Durable submission, SQS delivery, and generation/owner-fenced investigations."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from switchboard.aws_clients import aws_client
from switchboard.dynamodb import DynamoStore, encode, key
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket
from switchboard.investigation.runs import (
    InvestigationRun,
    get_investigation_run,
    require_run_access,
)
from switchboard.investigation.tools import InvestigationContext
from switchboard.storage import StorageError, WorkspaceStorage

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
TERMINAL = {"completed", "failed"}


class InvestigationJob(BaseModel):
    run_id: UUID
    ticket_id: str
    status: Literal["queued", "running", "completed", "failed"]
    status_url: str
    error: str | None = None
    retryable: bool = False


def database(storage: WorkspaceStorage) -> DynamoStore:
    if not isinstance(storage.transport, DynamoStore):
        raise RuntimeError("Queued investigations require DynamoDB")
    return storage.transport


def queue_client():
    return aws_client("sqs")


def submission_allowances(store: DynamoStore, workspace_id: str) -> list[dict]:
    """Charge a new job atomically with its records; retries cost no allowance."""
    now = datetime.now(timezone.utc)
    today = now.date()
    operations = []
    for partition, setting, period in [
        ("USAGE", "INVESTIGATION_DAILY_LIMIT", today.isoformat()),
        (
            f"USAGE#{workspace_id}",
            "INVESTIGATION_VISITOR_DAILY_LIMIT",
            today.isoformat(),
        ),
        (
            "USAGE",
            "INVESTIGATION_WINDOW_LIMIT",
            f"WINDOW#{int(now.timestamp()) // 300}",
        ),
    ]:
        limit = int(os.getenv(setting, "0"))
        if limit <= 0:
            continue

        operations.append(
            {
                "Update": {
                    "TableName": store.table,
                    "Key": key(partition, period),
                    "UpdateExpression": "SET expires_at = :expires ADD #count :one",
                    "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": {
                        ":one": {"N": "1"},
                        ":limit": {"N": str(limit)},
                        ":expires": {
                            "N": str(
                                int(
                                    datetime.combine(
                                        today, datetime.min.time(), timezone.utc
                                    ).timestamp()
                                )
                                + 3 * 86400
                            )
                        },
                    },
                }
            }
        )
    return operations


def require_daily_capacity(store: DynamoStore, allowances: list[dict]):
    """Explain a canceled transaction without rejecting an idempotent retry."""
    for operation in allowances:
        update = operation["Update"]
        response = store.client.get_item(
            TableName=store.table, Key=update["Key"], ConsistentRead=True
        )
        used = int(response.get("Item", {}).get("count", {}).get("N", "0"))
        limit = int(update["ExpressionAttributeValues"][":limit"]["N"])
        if used >= limit:
            if update["Key"]["SK"]["S"].startswith("WINDOW#"):
                raise StorageError(
                    "Too many new investigations. Please try again in a few minutes.",
                    status=429,
                )
            raise StorageError(
                "The demo's daily investigation limit has been reached. Try again tomorrow (UTC).",
                status=429,
            )


def authorize(storage: WorkspaceStorage, job: dict) -> None:
    metadata = database(storage).metadata(storage.workspace_id)
    if not metadata or metadata["generation"] != job["generation"]:
        raise PermissionError("Workspace has changed")
    if metadata["identityMode"] == "demo":
        activity = datetime.fromisoformat(metadata["updatedAt"]).timestamp()
        if time.time() - activity > 86400:
            raise PermissionError("Workspace has expired")
    context = InvestigationContext(
        storage=storage, employee_id=job["requester_employee_id"]
    )
    require_run_access(context=context, run=job)
    ticket = get_ticket(
        session=EmployeeSession(storage=storage, employee_id=context.employee_id),
        ticket_id=job["ticket_id"],
    )
    if ticket.assigned_employee_id != context.employee_id:
        raise PermissionError("Ticket assignment has changed")


def envelope(job: dict) -> InvestigationJob:
    return InvestigationJob(
        run_id=UUID(job["id"]),
        ticket_id=job["ticket_id"],
        status=job["status"],
        status_url=f"/api/investigations/{job['id']}",
        error=job.get("error"),
        retryable=job["status"] not in TERMINAL,
    )


def submit(
    *,
    storage: WorkspaceStorage,
    employee_id: str,
    ticket_id: str,
    request_key: UUID,
    sqs: Any = None,
) -> InvestigationJob:
    # 1. Authorize first and capture current access, independent of client claims.
    store = database(storage)
    partition = store.partition(storage.workspace_id)
    session = EmployeeSession(storage=storage, employee_id=employee_id)
    ticket = get_ticket(session=session, ticket_id=ticket_id)
    if ticket.assigned_employee_id != employee_id:
        raise PermissionError("Ticket unavailable")
    role = session.get_active_employee_role()
    request_sort = f"REQUEST#{employee_id}#{request_key}"
    active_key = f"ACTIVE#{employee_id}#{ticket_id}"
    pointer = store.get(partition, request_sort)
    if pointer and pointer["ticket_id"] != ticket_id:
        raise StorageError("Idempotency key has different inputs", status=409)

    # 2. Persist a job and its lookup before attempting delivery.
    existing_id = pointer["id"] if pointer else store.get(partition, active_key)
    job = store.get(partition, f"JOB#{existing_id}") if existing_id else None
    if job is None:
        allowances = submission_allowances(store, storage.workspace_id)
        identifier = str(uuid4())
        job = {
            "id": identifier,
            "ticket_id": ticket_id,
            "generation": store.generation(storage.workspace_id),
            "requester_employee_id": employee_id,
            "requester_role": role,
            "customer_ids": [ticket.customer_id],
            "status": "queued",
            "dispatch": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempt": 0,
            "active_key": active_key,
            "owner": None,
            "lease_until": 0,
        }
        try:
            store.transaction(
                storage.workspace_id,
                [
                    store.put(partition, f"JOB#{identifier}", job, absent=True),
                    store.put(
                        partition,
                        request_sort,
                        {"id": identifier, "ticket_id": ticket_id},
                        absent=True,
                    ),
                    store.put(partition, active_key, identifier, absent=True),
                    store.put(
                        partition,
                        f"JOBHISTORY#{ticket_id}#{job['created_at']}#{identifier}",
                        identifier,
                    ),
                    *allowances,
                ],
            )
        except StorageError as error:
            pointer = store.get(partition, request_sort)
            if pointer and pointer["ticket_id"] != ticket_id:
                raise StorageError(
                    "Idempotency key has different inputs", status=409
                ) from None
            identifier = pointer["id"] if pointer else store.get(partition, active_key)
            job = store.get(partition, f"JOB#{identifier}") if identifier else None
            if error.status == 409 and not job:
                require_daily_capacity(store, allowances)
            if error.status != 409 or not job:
                raise
    if not pointer:
        # Bind alternate keys to the canonical active job as well.
        try:
            store.transaction(
                storage.workspace_id,
                [
                    store.put(
                        partition,
                        request_sort,
                        {"id": job["id"], "ticket_id": ticket_id},
                        absent=True,
                    )
                ],
            )
        except StorageError as error:
            if error.status != 409:
                raise
            bound = store.get(partition, request_sort)
            if not bound or bound["id"] != job["id"] or bound["ticket_id"] != ticket_id:
                raise
    authorize(storage, job)
    if job["dispatch"] == "pending" and job["status"] == "queued":
        dispatch(storage=storage, job=job, sqs=sqs)
    return envelope(store.get(partition, f"JOB#{job['id']}"))


def dispatch(*, storage: WorkspaceStorage, job: dict, sqs: Any = None):
    from switchboard.workflow import start

    store = database(storage)
    try:
        if not start(storage, job["id"]):
            (sqs or queue_client()).send_message(
                QueueUrl=os.environ["INVESTIGATION_QUEUE_URL"],
                MessageBody=encode(
                    {
                        "workspace_id": storage.workspace_id,
                        "generation": job["generation"],
                        "run_id": job["id"],
                    }
                ),
            )
    except (BotoCoreError, ClientError):
        raise StorageError(
            f"Submission saved as {job['id']}; retry the same request to deliver it",
            status=503,
        ) from None
    try:
        store.transaction(
            storage.workspace_id,
            [
                store.replace(
                    store.partition(storage.workspace_id),
                    f"JOB#{job['id']}",
                    job,
                    {**job, "dispatch": "sent"},
                )
            ],
        )
    except StorageError as error:
        current = store.get(store.partition(storage.workspace_id), f"JOB#{job['id']}")
        if (
            error.status != 409
            or not current
            or (current["status"] == "queued" and current["dispatch"] == "pending")
        ):
            raise
        # The worker already claimed/completed it; never rewind its state.


def read(
    *, storage: WorkspaceStorage, employee_id: str, run_id: UUID
) -> InvestigationJob | InvestigationRun:
    store = database(storage)
    job = store.get(store.partition(storage.workspace_id), f"JOB#{run_id}")
    if not job:
        return get_investigation_run(
            storage=storage, employee_id=employee_id, run_id=run_id
        )
    if employee_id != job["requester_employee_id"]:
        raise PermissionError("Investigation unavailable")
    authorize(storage, job)
    if job["status"] == "completed":
        return get_investigation_run(
            storage=storage, employee_id=employee_id, run_id=run_id
        )
    return envelope(job)


def history(
    *, storage: WorkspaceStorage, employee_id: str, ticket_id: str
) -> list[dict]:
    store = database(storage)
    partition = store.partition(storage.workspace_id)
    result = []
    for identifier in reversed(store.query(partition, f"JOBHISTORY#{ticket_id}#")):
        job = store.get(partition, f"JOB#{identifier}")
        if job["status"] == "completed" or job["requester_employee_id"] != employee_id:
            continue
        try:
            authorize(storage, job)
        except PermissionError:
            continue
        result.append(
            {
                "run_id": job["id"],
                "ticket_id": ticket_id,
                "scenario_id": None,
                "outcome": job["status"],
            }
        )
    return result


def release_marker(store: DynamoStore, partition: str, job: dict) -> dict:
    operation = store.delete(partition, job["active_key"])
    operation["Delete"].update(
        ConditionExpression="#body = :identifier",
        ExpressionAttributeNames={"#body": "body"},
        ExpressionAttributeValues={":identifier": {"S": encode(job["id"])}},
    )
    return operation


def fail(storage: WorkspaceStorage, job: dict, message: str):
    store = database(storage)
    if job["status"] in TERMINAL:
        return
    partition = store.partition(storage.workspace_id)
    store.transaction(
        storage.workspace_id,
        [
            store.replace(
                partition,
                f"JOB#{job['id']}",
                job,
                {**job, "status": "failed", "error": message},
            ),
            release_marker(store, partition, job),
        ],
        completion=True,
        failure=True,
    )


def process_message(message: dict, context: Any = None):
    # 1. Resolve only a durable job; SQS never supplies employee authority.
    workspace_id = str(UUID(message["workspace_id"]))
    identifier = str(UUID(message["run_id"]))
    storage = WorkspaceStorage.from_environment(workspace_id=workspace_id)
    store = database(storage)
    metadata = store.metadata(workspace_id)
    if not metadata or metadata["generation"] != message["generation"]:
        return
    partition = store.partition(workspace_id)
    job = store.get(partition, f"JOB#{identifier}")
    if not job or job["status"] in TERMINAL:
        return
    if job["generation"] != message["generation"]:
        raise ValueError("Invalid queued generation")
    if job["lease_until"] > time.time():
        raise StorageError(
            "Investigation already running; retry after lease", status=503
        )

    # 2. Claim with a lease beyond the hard invocation deadline.
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 300
    claimed = {
        **job,
        "status": "running",
        "dispatch": "sent",
        "owner": str(uuid4()),
        "lease_until": time.time() + remaining + 30,
        "attempt": job["attempt"] + 1,
    }
    store.transaction(
        workspace_id, [store.replace(partition, f"JOB#{identifier}", job, claimed)]
    )
    store.lease = claimed
    logger.info(
        "Investigation claimed",
        extra={
            "run_id": identifier,
            "ticket_id": job["ticket_id"],
            "stage": "claim",
            "attempt": claimed["attempt"],
        },
    )

    # 3. Revalidate access and run once; SQS controls transient retries.
    from switchboard.investigation.agent import create_model
    from switchboard.investigation.deadline import investigation_deadline
    from switchboard.investigation.fixtures import investigate_ticket_fixture
    from switchboard.investigation.mode import get_investigation_mode
    from switchboard.investigation.report_validation import (
        EmptyModelResponseError,
        ReportValidationError,
    )
    from switchboard.investigation.runner import investigate_ticket

    try:
        authorize(storage, claimed)
        with investigation_deadline(time.monotonic() + remaining - 15):
            arguments = {
                "ticket_id": job["ticket_id"],
                "employee_id": job["requester_employee_id"],
                "storage": storage,
                "now": datetime.now(timezone.utc),
                "run_id": UUID(identifier),
            }
            if get_investigation_mode() == "fixture":
                investigate_ticket_fixture(**arguments)
            else:
                investigate_ticket(model=create_model(), **arguments)
    except (PermissionError, ValueError) as error:
        fail(
            storage,
            claimed,
            "Current access or investigation output no longer permits this request.",
        )
        rejection_details = {
            "run_id": identifier,
            "stage": "failed",
            "attempt": claimed["attempt"],
            "error_type": type(error).__name__,
        }
        if isinstance(error, ReportValidationError):
            rejection_details["error_reason"] = str(error)
            rejection_details["report_validation"] = error.diagnostics

        logger.info("Investigation rejected", extra=rejection_details)
        return
    except Exception as error:
        retry_details = {
            "run_id": identifier,
            "stage": "retry",
            "attempt": claimed["attempt"],
            "error_type": type(error).__name__,
        }
        if isinstance(error, EmptyModelResponseError):
            retry_details["error_reason"] = str(error)
            retry_details["report_validation"] = error.diagnostics

        logger.warning(
            "Investigation awaits SQS retry",
            extra=retry_details,
        )
        raise
    logger.info(
        "Investigation completed",
        extra={
            "run_id": identifier,
            "stage": "completed",
            "attempt": claimed["attempt"],
        },
    )


def worker_handler(event: dict, context: Any):
    from switchboard.workflow import complete_investigation

    for record in event["Records"]:
        message = json.loads(record["body"])
        process_message(message, context)
        complete_investigation(message)
