from datetime import datetime, timezone

import pytest

from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.storage import StorageError, WorkspaceStorage


def test_client_sends_only_named_operation_and_workspace():
    calls = []

    def transport(operation: str, workspace_id: str, payload: dict):
        calls.append((operation, workspace_id, payload))
        return {"id": payload["id"]}

    storage = WorkspaceStorage(
        workspace_id="workspace-one",
        transport=transport,
    )

    assert storage.get_ticket(ticket_id="CHG-1042") == {"id": "CHG-1042"}
    assert calls == [("ticket.get", "workspace-one", {"id": "CHG-1042"})]


def test_workspaces_cannot_read_each_others_records(memory_store):
    first = memory_store.storage(workspace_id="first")
    second = memory_store.storage(workspace_id="second")
    payload = build_workspace_payload(
        scenario_id="baseline",
        selected_scenario=load_scenarios()["baseline"],
        identity_mode="eval",
        updated_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )
    first.reset_workspace(payload=payload)

    assert first.get_ticket(ticket_id="CHG-1042") is not None
    assert second.get_ticket(ticket_id="CHG-1042") is None


def test_failed_execution_keeps_configuration_and_receipt_together(storage):
    execution = {
        "id": "execution-one",
        "proposal_id": "proposal-one",
        "executed_by_employee_id": "emp-alex",
        "approval_id": "approval-one",
        "executed_at": "2026-09-22T14:15:00+00:00",
        "previous_configuration_version": 8,
        "resulting_configuration_version": 9,
    }

    with pytest.raises(StorageError, match="Configuration changed"):
        storage.apply_execution(
            execution=execution,
            integration_id="int-acme-prod",
            customer_id="acme",
            expected_version=8,
            current_endpoint="https://old.acme.example/deals",
            proposed_endpoint="https://events.acme.example/deals",
        )

    integration = storage.get_integration(integration_id="int-acme-prod")
    assert integration["endpoint"] == "https://old.acme.example/deals"
    assert integration["version"] == 7
    assert storage.get_execution(proposal_id="proposal-one") is None


def test_failed_verification_keeps_ticket_and_receipt_together(storage):
    verification = {
        "id": "verification-one",
        "execution_id": "missing-execution",
        "proposal_id": "missing-proposal",
        "verified_by_employee_id": "emp-alex",
        "outcome": "delivered",
        "test_event_id": "test-event-one",
        "destination": "https://events.acme.example/deals",
        "evidence": "Synthetic event accepted.",
        "verified_at": "2026-09-22T14:16:00+00:00",
    }

    with pytest.raises(StorageError, match="Execution unavailable"):
        storage.record_delivery_verification(verification=verification)

    assert storage.get_ticket(ticket_id="CHG-1042")["status"] == "open"
    assert storage.get_delivery_verification(execution_id="missing-execution") is None
