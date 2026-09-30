"""Exercise transactional storage through DynamoDB's API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from switchboard.change_management import approve_proposal, execute_proposal
from switchboard.delivery_verification import verify_execution_delivery
from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.dynamodb import DynamoStore
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.storage import StorageError, WorkspaceStorage


@pytest.fixture
def dynamo():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="switchboard",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"} for name in ("PK", "SK")
            ],
            ProvisionedThroughput={"ReadCapacityUnits": 25, "WriteCapacityUnits": 25},
        )
        yield DynamoStore("switchboard", client)


def workspace(dynamo, identifier="one"):
    storage = WorkspaceStorage(identifier, transport=dynamo)
    storage.reset_workspace(
        payload=build_workspace_payload(
            scenario_id="baseline",
            selected_scenario=load_scenarios()["baseline"],
            identity_mode="eval",
        )
    )
    return storage


def test_full_portfolio_and_atomic_execution(dynamo):
    storage = WorkspaceStorage("one", transport=dynamo)
    initialize_demo_portfolio(storage=storage)
    proposal = next(
        item
        for item in storage.list_pending_proposals()
        if item["ticket_id"] == "CHG-1045"
    )
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    reviewer = EmployeeSession(storage=storage, employee_id="emp-priya")
    with pytest.raises(PermissionError):
        approve_proposal(proposal_id=proposal["id"], session=author)
    approve_proposal(proposal_id=proposal["id"], session=reviewer)
    receipt = execute_proposal(
        proposal_id=proposal["id"],
        session=author,
        executed_at=datetime.now(timezone.utc),
    )
    repeat = execute_proposal(
        proposal_id=proposal["id"],
        session=author,
        executed_at=datetime.now(timezone.utc),
    )
    assert receipt.was_created and not repeat.was_created
    assert receipt.execution.id == repeat.execution.id
    integration = storage.get_integration(integration_id=proposal["integration_id"])
    assert integration is not None
    assert integration["version"] == proposal["expected_configuration_version"] + 1
    verified = verify_execution_delivery(
        proposal_id=proposal["id"],
        session=author,
        verified_at=datetime.now(timezone.utc),
    )
    assert verified.was_created
    ticket = storage.get_ticket(ticket_id="CHG-1045")
    assert ticket is not None and ticket["status"] == "closed"


def test_concurrent_proposal_and_receipt_canonicalization(dynamo):
    storage = workspace(dynamo)
    proposal = {
        "id": "p1",
        "created_at": "2026-09-30T00:00:00Z",
        "ticket_id": "CHG-1042",
        "customer_id": "acme",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda identifier: storage.save_proposal(
                    proposal={**proposal, "id": identifier}
                ),
                ("p1", "p2"),
            )
        )
    assert results[0]["proposal"]["id"] == results[1]["proposal"]["id"]
    assert sum(result["wasCreated"] for result in results) == 1
    integration = storage.get_integration(integration_id="int-acme-prod")
    assert integration is not None
    arguments: dict[str, Any] = dict(
        integration_id=integration["id"],
        customer_id=integration["customer_id"],
        expected_version=integration["version"],
        current_endpoint=integration["endpoint"],
        proposed_endpoint="https://new.acme.example/orders",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda identifier: storage.apply_execution(
                    execution={"id": identifier, "proposal_id": "p1"}, **arguments
                ),
                ("e1", "e2"),
            )
        )
    assert receipts[0]["execution"] == receipts[1]["execution"]
    assert sum(item["wasCreated"] for item in receipts) == 1
    with pytest.raises(StorageError, match="Configuration changed"):
        storage.apply_execution(
            execution={"id": "e3", "proposal_id": "p3"}, **arguments
        )


def test_reset_fences_old_requests_and_failed_seed_preserves_previous(dynamo):
    storage = workspace(dynamo)
    previous = storage.get_workspace()
    with pytest.raises(RuntimeError), storage.initialization():
        storage.reset_workspace(
            payload=build_workspace_payload(
                scenario_id="baseline",
                selected_scenario=load_scenarios()["baseline"],
                identity_mode="eval",
            )
        )
        assert DynamoStore(dynamo.table, dynamo.client).metadata("one") == previous
        raise RuntimeError("Seed failed")
    assert dynamo.metadata("one") == previous
    old = DynamoStore(dynamo.table, dynamo.client)
    old_storage = WorkspaceStorage("one", transport=old)
    old_storage.get_workspace()
    workspace(dynamo)
    with pytest.raises(StorageError, match="Workspace has changed"):
        old_storage.save_proposal(proposal={"id": "p1", "created_at": "now"})
    assert (
        WorkspaceStorage(
            "two", transport=DynamoStore(dynamo.table, dynamo.client)
        ).get_workspace()
        is None
    )


def test_chunked_results_and_history(dynamo):
    storage = workspace(dynamo)
    text = "á🙂" * 90000
    run = {
        "id": str(uuid4()),
        "ticket_id": "CHG-1042",
        "created_at": "2026-09-30T12:00:00Z",
        "result": {"text": text, "proposal": {"id": "p1"}},
    }
    storage.save_run(run=run)
    assert storage.get_run(run_id=run["id"]) == run
    assert storage.list_runs(ticket_id="CHG-1042") == [run]
    assert storage.find_proposal_run(proposal_id="p1") == run["id"]
    manifest = dynamo.get(dynamo.partition("one"), f"RUN#{run['id']}")["manifest"]
    dynamo.client.delete_item(
        TableName=dynamo.table,
        Key={
            "PK": {"S": dynamo.partition("one")},
            "SK": {"S": f"CHUNK#RUN#{run['id']}#{manifest['version']}#0000"},
        },
    )
    with pytest.raises(StorageError, match="Incomplete"):
        storage.get_run(run_id=run["id"])


def test_transaction_conflict_retries_with_one_token(dynamo, monkeypatch):

    # 1. Inject two temporary conflicts into a real transactional workspace.
    storage = workspace(dynamo)
    original = dynamo.client.transact_write_items
    conflict = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "TransactionConflict"}],
        },
        "TransactWriteItems",
    )
    retrying_write = Mock(side_effect=[conflict, conflict, None])
    monkeypatch.setattr(dynamo.client, "transact_write_items", retrying_write)
    monkeypatch.setattr("switchboard.dynamodb.time.sleep", lambda seconds: None)
    operation = dynamo.put(
        dynamo.partition(storage.workspace_id), "TEST", {"saved": True}
    )

    # 2. Retry the same guarded transaction until the lock conflict clears.
    dynamo.transaction(storage.workspace_id, [operation])

    # 3. Verify one request token and the resulting stored record.
    calls = retrying_write.call_args_list
    assert len(calls) == 3
    assert len({call.kwargs["ClientRequestToken"] for call in calls}) == 1
    original(**calls[-1].kwargs)
    assert dynamo.get(dynamo.partition(storage.workspace_id), "TEST") == {"saved": True}


@pytest.mark.parametrize(
    "reason_codes, expected_status, expected_calls",
    [
        (["ConditionalCheckFailed"], 409, 1),
        (["TransactionConflict", "ConditionalCheckFailed"], 409, 1),
        (["ValidationError"], 503, 1),
        (["TransactionConflict"], 503, 4),
    ],
)
def test_transaction_retry_keeps_conditions_and_has_a_bound(
    dynamo, monkeypatch, reason_codes, expected_status, expected_calls
):

    # 1. Inject the cancellation reason without changing the workspace.
    storage = workspace(dynamo)
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
        },
        "TransactWriteItems",
    )
    failed_write = Mock(side_effect=error)
    monkeypatch.setattr(dynamo.client, "transact_write_items", failed_write)
    monkeypatch.setattr("switchboard.dynamodb.time.sleep", lambda seconds: None)
    operation = dynamo.put(
        dynamo.partition(storage.workspace_id), "TEST", {"saved": True}
    )

    # 2. Attempt the guarded write and retain its final error.
    with pytest.raises(StorageError) as raised:
        dynamo.transaction(storage.workspace_id, [operation])

    # 3. Confirm bounded retries and that no rejected write became visible.
    assert raised.value.status == expected_status
    assert failed_write.call_count == expected_calls
    assert dynamo.get(dynamo.partition(storage.workspace_id), "TEST") is None
