import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

import switchboard.api.investigations as investigation_api
from switchboard import api
from switchboard.api import RequestContext
from switchboard.demo.workspaces import open_demo_workspace
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket


@pytest.fixture(autouse=True)
def demo_auth_mode(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AUTH_MODE", "demo")


def client_for(storage, *, employee_id: str = "emp-alex") -> TestClient:
    api.app.dependency_overrides[api.get_request_context] = lambda: RequestContext(
        identity_mode="demo",
        employee_id=employee_id,
        workspace_id=storage.workspace_id,
        storage=storage,
    )
    return TestClient(api.app)


class StubInvestigator:
    def __init__(self, report: dict, messages=None):
        self.report = report
        self.messages = messages or []

    def invoke(self, *_args, **_kwargs):
        return {
            "messages": [*self.messages, AIMessage(content=json.dumps(self.report))]
        }


def proposal_candidate_report() -> dict:
    return {
        "outcome": "proposal_candidate",
        "ticket_id": "CHG-1042",
        "proposed_endpoint": "https://events.acme.example/deals",
        "evidence_ids": [
            "CHG-1042",
            "acme",
            "int-acme-prod",
            "endpoint-change-v2",
        ],
        "findings": {
            "overview": "Current records support preparing the requested change.",
            "decision_criteria": [],
            "recommendation": "Prepare the proposal for independent review.",
        },
        "blockers": [],
    }


def policy_issue_response() -> str:
    return json.dumps(
        {
            "issues": [
                {
                    "claim": "Rollback is never allowed.",
                    "policy_id": "endpoint-change-v2",
                    "policy_excerpt": (
                        "If verification fails, restore the old endpoint only when the "
                        "approved plan permits it and no intervening change makes restoration unsafe."
                    ),
                    "issue_kind": "invented_absolute_prohibition",
                    "explanation": "The policy permits a conditional restoration.",
                }
            ]
        }
    )


def blocked_report() -> dict:
    return {
        "outcome": "blocked",
        "ticket_id": "CHG-1042",
        "proposed_endpoint": None,
        "evidence_ids": ["CHG-1042", "endpoint-change-v2"],
        "findings": {
            "overview": "The integration record could not be retrieved.",
            "decision_criteria": [
                {
                    "name": "Destination registration",
                    "status": "unavailable",
                    "required_before": "proposal",
                    "explanation": "The integration record was unavailable.",
                    "policy_id": "endpoint-change-v2",
                    "evidence_ids": [],
                }
            ],
            "recommendation": "Restore access and investigate again.",
        },
        "blockers": [
            {
                "kind": "missing_evidence",
                "summary": "Integration evidence is unavailable.",
                "resolution": "Restore access to the integration record.",
            }
        ],
    }


def test_ticket_and_identity_routes_use_the_request_workspace(storage):
    with client_for(storage) as client:
        identity = client.get("/api/me")
        tickets = client.get("/api/tickets")
    api.app.dependency_overrides.clear()

    assert identity.status_code == 200
    assert identity.json() == {
        "employee_id": "emp-alex",
        "name": "Alex Rivera",
        "role": "implementation_engineer",
    }
    assert tickets.status_code == 200
    assert tickets.json()[0]["requester"]["name"] == "Jordan Lee"


def test_inaccessible_ticket_is_indistinguishable_from_missing(storage):
    with client_for(storage, employee_id="emp-ben") as client:
        inaccessible = client.get("/api/tickets/CHG-1042")
        missing = client.get("/api/tickets/missing")
    api.app.dependency_overrides.clear()

    assert inaccessible.status_code == 404
    assert missing.status_code == 404
    assert inaccessible.json() == missing.json() == {"detail": "Ticket unavailable"}

    with client_for(storage, employee_id="emp-priya") as client:
        unassigned = client.get("/api/tickets/CHG-1042")
    api.app.dependency_overrides.clear()
    assert unassigned.status_code == 404
    assert unassigned.json() == {"detail": "Ticket unavailable"}


def test_demo_case_endpoints_are_removed(storage):
    api.app.dependency_overrides[api.get_request_context] = lambda: RequestContext(
        identity_mode="demo",
        employee_id="emp-alex",
        workspace_id=storage.workspace_id,
        storage=storage,
    )
    with TestClient(api.app) as client:
        catalog = client.get("/api/demo/cases")
        prepare = client.post("/api/demo/cases/valid-request/prepare")
    api.app.dependency_overrides.clear()

    assert catalog.status_code == 404
    assert prepare.status_code == 404


def test_ticket_summaries_include_actor_specific_workflow_state(memory_store):
    base = memory_store.storage(workspace_id="unused")
    storage = open_demo_workspace(
        workspace_id=None, base_storage=base
    ).workspace.storage

    with client_for(storage, employee_id="emp-alex") as client:
        alex_tickets = client.get("/api/tickets")
    api.app.dependency_overrides.clear()
    with client_for(storage, employee_id="emp-ben") as client:
        ben_tickets = client.get("/api/tickets")
    api.app.dependency_overrides.clear()

    assert alex_tickets.status_code == 200
    alex_by_id = {item["id"]: item for item in alex_tickets.json()}
    assert set(alex_by_id) == {"CHG-1042", "CHG-1044", "CHG-1045", "CHG-1046"}
    assert alex_by_id["CHG-1042"]["workflow_status"]["code"] == "ready_to_investigate"
    assert alex_by_id["CHG-1045"]["workflow_status"]["code"] == "awaiting_approval"
    assert alex_by_id["CHG-1045"]["needs_attention"] is False
    assert alex_by_id["CHG-1046"]["workflow_status"]["code"] == "approval_recorded"
    assert alex_by_id["CHG-1046"]["needs_attention"] is True
    assert [item["id"] for item in ben_tickets.json()] == ["CHG-1043"]


def test_pending_proposal_appears_only_in_the_eligible_reviewer_inbox(memory_store):
    base = memory_store.storage(workspace_id="unused")
    storage = open_demo_workspace(
        workspace_id=None, base_storage=base
    ).workspace.storage

    with client_for(storage, employee_id="emp-alex") as client:
        alex_inbox = client.get("/api/approvals")
    api.app.dependency_overrides.clear()
    with client_for(storage, employee_id="emp-priya") as client:
        priya_inbox = client.get("/api/approvals")
    api.app.dependency_overrides.clear()

    assert alex_inbox.json() == []
    assert [item["proposal"]["ticket_id"] for item in priya_inbox.json()] == [
        "CHG-1045"
    ]


def test_investigation_is_validated_before_the_proposal_is_saved(storage, monkeypatch):
    policy_model = GenericFakeChatModel(
        messages=iter([AIMessage(content='{"issues": []}')])
    )
    monkeypatch.setattr(investigation_api, "create_model", lambda: policy_model)
    monkeypatch.setattr(
        "switchboard.investigation.runner.build_agent",
        lambda **_kwargs: StubInvestigator(proposal_candidate_report()),
    )

    with client_for(storage) as client:
        response = client.post("/api/investigations", json={"ticket_id": "CHG-1042"})
    api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["result"]["report_validation"] == {
        "policy_ids": ["endpoint-change-v1", "endpoint-change-v2"],
        "evaluation_count": 1,
        "revision_count": 0,
    }
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1
    assert len(storage.list_pending_proposals()) == 1


def test_local_fixture_investigation_uses_real_storage_without_a_model(
    storage, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_INVESTIGATION_MODE", "fixture")
    monkeypatch.setenv("SWITCHBOARD_RUNTIME", "local")
    monkeypatch.setattr(
        investigation_api,
        "create_model",
        lambda: pytest.fail("fixture investigations must not create a model"),
    )

    with client_for(storage) as client:
        response = client.post("/api/investigations", json={"ticket_id": "CHG-1042"})
    api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["result"]["source"] == "fixture"
    assert response.json()["result"]["proposal"] is not None
    assert {item["id"] for item in response.json()["result"]["evidence"]} == {
        "CHG-1042",
        "acme",
        "int-acme-prod",
        "endpoint-change-v1",
        "endpoint-change-v2",
    }
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1
    assert len(storage.list_pending_proposals()) == 1


def test_saved_evidence_route_returns_the_snapshot_only_to_the_run_owner(
    storage, monkeypatch
):
    policy_model = GenericFakeChatModel(
        messages=iter([AIMessage(content='{"issues": []}')])
    )
    ticket = get_ticket(
        session=EmployeeSession(storage=storage, employee_id="emp-alex"),
        ticket_id="CHG-1042",
    )
    investigator = StubInvestigator(
        proposal_candidate_report(),
        messages=[
            ToolMessage(
                name="get_ticket",
                tool_call_id="ticket-call",
                content=ticket.model_dump_json(),
            )
        ],
    )
    monkeypatch.setattr(investigation_api, "create_model", lambda: policy_model)
    monkeypatch.setattr(
        "switchboard.investigation.runner.build_agent",
        lambda **_kwargs: investigator,
    )

    with client_for(storage) as client:
        run = client.post("/api/investigations", json={"ticket_id": "CHG-1042"})
        evidence = client.get(
            f"/api/investigations/{run.json()['run_id']}/evidence/CHG-1042"
        )
    api.app.dependency_overrides.clear()

    with client_for(storage, employee_id="emp-ben") as client:
        inaccessible = client.get(
            f"/api/investigations/{run.json()['run_id']}/evidence/CHG-1042"
        )
        missing = client.get(
            f"/api/investigations/{run.json()['run_id']}/evidence/missing"
        )
    api.app.dependency_overrides.clear()

    assert evidence.status_code == 200
    assert evidence.json()["snapshot"]["document"]["id"] == "CHG-1042"
    assert evidence.json()["has_changed"] is False
    assert inaccessible.status_code == missing.status_code == 404
    assert inaccessible.json() == missing.json() == {"detail": "Evidence unavailable"}


def test_failed_report_validation_saves_neither_run_nor_proposal(storage, monkeypatch):
    report = proposal_candidate_report()
    report["findings"]["overview"] = "Rollback is never allowed."
    policy_model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(content=policy_issue_response()),
                AIMessage(content=json.dumps(report["findings"])),
                AIMessage(content=policy_issue_response()),
            ]
        )
    )
    monkeypatch.setattr(investigation_api, "create_model", lambda: policy_model)
    monkeypatch.setattr(
        "switchboard.investigation.runner.build_agent",
        lambda **_kwargs: StubInvestigator(report),
    )

    with client_for(storage) as client:
        response = client.post("/api/investigations", json={"ticket_id": "CHG-1042"})
    api.app.dependency_overrides.clear()

    assert response.status_code == 502
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 0
    assert len(storage.list_pending_proposals()) == 0


def test_validated_blocked_investigation_is_saved_without_a_proposal(
    storage, monkeypatch
):
    policy_model = GenericFakeChatModel(
        messages=iter([AIMessage(content='{"issues": []}')])
    )
    monkeypatch.setattr(investigation_api, "create_model", lambda: policy_model)
    monkeypatch.setattr(
        "switchboard.investigation.runner.build_agent",
        lambda **_kwargs: StubInvestigator(blocked_report()),
    )

    with client_for(storage) as client:
        response = client.post("/api/investigations", json={"ticket_id": "CHG-1042"})
    api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["result"]["investigation"]["outcome"] == "blocked"
    assert response.json()["result"]["proposal"] is None
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1
    assert len(storage.list_pending_proposals()) == 0


def test_manual_policy_review_endpoint_is_removed(storage):
    with client_for(storage) as client:
        response = client.post(
            "/api/investigations/00000000-0000-0000-0000-000000000001/policy-review"
        )
    api.app.dependency_overrides.clear()

    assert response.status_code == 404


def test_executed_proposal_can_be_verified_and_reviewed(memory_store):
    base = memory_store.storage(workspace_id="unused")
    storage = open_demo_workspace(
        workspace_id=None, base_storage=base
    ).workspace.storage
    run = storage.list_runs(ticket_id="CHG-1046")[0]
    proposal_id = run["result"]["proposal"]["id"]
    run_id = run["id"]
    with client_for(storage) as client:
        base = f"/api/runs/{run_id}/proposals/{proposal_id}"

        execution = client.post(f"{base}/execution")
        verification = client.post(f"{base}/verification")
        review = client.get(base)
        ticket = client.get("/api/tickets/CHG-1046")
    api.app.dependency_overrides.clear()

    assert execution.status_code == 200
    assert verification.status_code == 200
    assert verification.json()["was_created"] is True
    assert verification.json()["verification"]["outcome"] == "delivered"
    assert review.json()["current_status"]["code"] == "delivery_verified"
    assert review.json()["verification"] == verification.json()["verification"]
    assert ticket.json()["status"] == "closed"
