"""Callbacks follow saved receipts; races and stale generations do not bypass them."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from switchboard import workflow
from switchboard.change_management import approve_proposal, execute_proposal
from switchboard.delivery_verification import verify_execution_delivery
from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.storage import WorkspaceStorage
from tests.test_dynamodb import dynamo as dynamo


@pytest.fixture
def change(dynamo, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DYNAMODB_TABLE", "switchboard")
    monkeypatch.setenv(
        "SWITCHBOARD_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:switchboard-change-workflow",
    )
    original = workflow.aws_client
    client = Mock()
    monkeypatch.setattr(
        workflow,
        "aws_client",
        lambda name, **kwargs: (
            client if name == "stepfunctions" else original(name, **kwargs)
        ),
    )
    storage = WorkspaceStorage(str(uuid4()), transport=dynamo)
    initialize_demo_portfolio(storage=storage)
    proposal = next(
        item
        for item in storage.list_pending_proposals()
        if item["ticket_id"] == "CHG-1045"
    )
    run_id = storage.find_proposal_run(proposal_id=proposal["id"])
    assert run_id
    payload = {
        "workspace_id": storage.workspace_id,
        "generation": dynamo.generation(storage.workspace_id),
        "run_id": run_id,
    }
    return storage, proposal, payload, client


def register(payload, stage):
    return workflow.handle(
        {
            "operation": "wait",
            "stage": stage,
            "workflow": payload,
            "task_token": stage + "-token",
        }
    )


def test_manual_workflow_waits_for_authorized_receipts_and_recovers_lost_callback(
    change, delivered_delivery
):
    storage, proposal, payload, client = change
    assert workflow.start(storage, payload["run_id"])
    assert json.loads(client.start_execution.call_args.kwargs["input"]) == payload
    register(payload, "approval")
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    with pytest.raises(PermissionError):
        approve_proposal(proposal_id=proposal["id"], session=author)
    workflow.advance(storage, payload["run_id"], "approval")
    client.send_task_success.assert_not_called()
    approve_proposal(
        proposal_id=proposal["id"],
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    client.send_task_success.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable"}}, "SendTaskSuccess"
    )
    with pytest.raises(ClientError):
        workflow.signal(storage, UUID(payload["run_id"]), "approval")
    client.send_task_success.side_effect = None
    workflow.advance(storage, payload["run_id"], "approval")
    assert client.send_task_success.call_args.kwargs["taskToken"] == "approval-token"
    assert storage.get_execution(proposal_id=proposal["id"]) is None

    # An execution committed before registration still releases the next wait.
    execute_proposal(
        proposal_id=proposal["id"],
        session=author,
        executed_at=datetime.now(timezone.utc),
    )
    register(payload, "execution")
    assert client.send_task_success.call_args.kwargs["taskToken"] == "execution-token"
    register(payload, "verification")
    verify_execution_delivery(
        proposal_id=proposal["id"],
        session=author,
        verified_at=datetime.now(timezone.utc),
    )
    workflow.signal(storage, UUID(payload["run_id"]), "verification")
    assert (
        json.loads(client.send_task_success.call_args.kwargs["output"])["outcome"]
        == "delivered"
    )
    count = client.send_task_success.call_count
    workflow.signal(storage, UUID(payload["run_id"]), "verification")
    assert client.send_task_success.call_count == count


def test_duplicate_completion_and_retired_generation_callbacks(change):
    storage, _, payload, client = change
    workflow.complete_investigation({**payload, "task_token": "investigation-token"})
    output = json.loads(client.send_task_success.call_args.kwargs["output"])
    assert (
        output["outcome"] == "proposal_candidate"
        and output["environment"] == "production"
    )
    assert "result" not in output and "token" not in output
    client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution"
    )
    assert workflow.start(storage, payload["run_id"])
    initialize_demo_portfolio(storage=storage)
    with pytest.raises(PermissionError, match="Workspace has changed"):
        register(payload, "approval")
    workflow.complete_investigation({**payload, "task_token": "retired-token"})
    client.send_task_failure.assert_called_with(
        taskToken="retired-token", error="InvestigationUnavailable"
    )
