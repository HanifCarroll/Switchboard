from datetime import datetime, timezone

import pytest

from switchboard.demo.workspaces import open_demo_workspace
from switchboard.investigation.fixtures import investigate_ticket_fixture
from switchboard.storage import WorkspaceStorage


def test_new_visitors_receive_isolated_baseline_workspaces(memory_store):
    base = memory_store.storage(workspace_id="unused")

    first = open_demo_workspace(workspace_id=None, base_storage=base)
    second = open_demo_workspace(workspace_id=None, base_storage=base)

    assert first.was_created is True
    assert second.was_created is True
    assert first.workspace.id != second.workspace.id
    first.workspace.storage.touch_workspace(updated_at="2026-09-20T00:00:00+00:00")
    second_workspace = second.workspace.storage.get_workspace()
    assert second_workspace is not None
    assert second_workspace["updatedAt"] != "2026-09-20T00:00:00+00:00"


def test_new_demo_workspace_contains_five_independent_requests(memory_store):
    base = memory_store.storage(workspace_id="unused")
    opened = open_demo_workspace(workspace_id=None, base_storage=base)

    tickets = opened.workspace.storage.list_tickets()

    assert {ticket["id"] for ticket in tickets} == {
        "CHG-1042",
        "CHG-1043",
        "CHG-1044",
        "CHG-1045",
        "CHG-1046",
    }
    assert len({ticket["integration_id"] for ticket in tickets}) == 5
    assert all(
        opened.workspace.storage.get_integration(
            integration_id=ticket["integration_id"]
        )
        is not None
        for ticket in tickets
    )


def test_known_workspace_reopens_without_resetting_records(memory_store):
    base = memory_store.storage(workspace_id="unused")
    created = open_demo_workspace(workspace_id=None, base_storage=base)

    reopened = open_demo_workspace(
        workspace_id=created.workspace.id,
        base_storage=base,
    )

    assert reopened.was_created is False
    assert reopened.workspace.id == created.workspace.id
    assert reopened.workspace.storage.get_ticket(ticket_id="CHG-1042") is not None


def test_failed_portfolio_seed_removes_the_partial_workspace(memory_store):
    operations: list[tuple[str, str]] = []

    def fail_during_seed(operation: str, workspace_id: str, payload: dict):
        operations.append((operation, workspace_id))
        if operation == "proposal.save":
            raise RuntimeError("seed failed")
        return memory_store.handle(operation, workspace_id, payload)

    base = WorkspaceStorage(
        workspace_id="unused",
        transport=fail_during_seed,
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        open_demo_workspace(workspace_id=None, base_storage=base)

    created_workspace_id = next(
        workspace_id
        for operation, workspace_id in operations
        if operation == "workspace.reset"
    )
    assert operations[-1] == ("workspace.delete", created_workspace_id)
    assert (
        memory_store.storage(workspace_id=created_workspace_id).get_workspace() is None
    )


def test_unsafe_request_has_an_immediate_blocked_fixture_result(memory_store):
    base = memory_store.storage(workspace_id="unused")
    storage = open_demo_workspace(
        workspace_id=None, base_storage=base
    ).workspace.storage

    result = investigate_ticket_fixture(
        ticket_id="CHG-1043",
        employee_id="emp-ben",
        storage=storage,
        now=datetime.now(timezone.utc),
    )

    assert result.result.source == "fixture"
    assert result.result.investigation.outcome == "blocked"
    assert result.result.proposal is None


def test_unauthorized_requester_is_blocked_without_a_proposal(memory_store):
    base = memory_store.storage(workspace_id="unused")
    storage = open_demo_workspace(
        workspace_id=None, base_storage=base
    ).workspace.storage

    result = investigate_ticket_fixture(
        ticket_id="CHG-1044",
        employee_id="emp-alex",
        storage=storage,
        now=datetime.now(timezone.utc),
    )

    assert result.result.investigation.outcome == "blocked"
    assert result.result.proposal is None
