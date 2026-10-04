"""Demo-only identity, predefined profiles, and isolated workspace boundaries."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from switchboard import api, auth
from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.storage import WorkspaceStorage


def test_demo_is_the_default_without_identity_provider_settings(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_AUTH_MODE", raising=False)
    assert auth.get_auth_mode() == "demo"


@pytest.mark.parametrize("mode", ["entra", "hybrid", "production", "typo", ""])
def test_legacy_non_demo_configuration_fails_closed(monkeypatch, mode):
    monkeypatch.setenv("SWITCHBOARD_AUTH_MODE", mode)
    with pytest.raises(RuntimeError, match="only supports"):
        auth.get_auth_mode()


@pytest.fixture
def demo_client(monkeypatch, memory_store):
    monkeypatch.setenv("SWITCHBOARD_AUTH_MODE", "demo")
    api.app.dependency_overrides.clear()
    monkeypatch.setattr(
        WorkspaceStorage,
        "from_environment",
        lambda *, workspace_id: memory_store.storage(workspace_id=workspace_id),
    )
    with TestClient(api.app) as client:
        yield client
    api.app.dependency_overrides.clear()


@pytest.mark.parametrize("header", ["Authorization", "X-Switchboard-Authorization"])
def test_account_tokens_are_rejected_before_opening_storage(
    demo_client, monkeypatch, header
):
    def unavailable(**_kwargs):
        pytest.fail("Account credentials must not reach any workspace")

    monkeypatch.setattr(WorkspaceStorage, "from_environment", unavailable)
    response = demo_client.get(
        "/api/me", headers={header: "Bearer token", "X-Demo-Persona-Id": "emp-alex"}
    )
    assert response.status_code == 400
    assert not demo_client.cookies


@pytest.mark.parametrize("employee_id", ["emp-real", "emp-admin", "", "entra-emp-alex"])
def test_unknown_profiles_are_rejected_before_opening_storage(
    demo_client, monkeypatch, employee_id
):
    def unavailable(**_kwargs):
        pytest.fail("Unknown profiles must not reach any workspace")

    monkeypatch.setattr(WorkspaceStorage, "from_environment", unavailable)
    response = demo_client.get("/api/me", headers={"X-Demo-Persona-Id": employee_id})
    assert response.status_code == 403
    assert not demo_client.cookies


def test_builtin_profiles_keep_roles_assignments_and_the_visitor_workspace(demo_client):
    alex = demo_client.get("/api/me")
    workspace = alex.headers["X-Switchboard-Workspace"]
    assert alex.json()["role"] == "implementation_engineer"
    assert demo_client.get("/api/approvals").json() == []
    priya = {"X-Demo-Persona-Id": "emp-priya"}
    assert demo_client.get("/api/me", headers=priya).json()["role"] == "technical_lead"
    assert (
        demo_client.get("/api/me", headers=priya).headers["X-Switchboard-Workspace"]
        == workspace
    )
    assert [
        item["proposal"]["ticket_id"]
        for item in demo_client.get("/api/approvals", headers=priya).json()
    ] == ["CHG-1045"]
    ben = {"X-Demo-Persona-Id": "emp-ben"}
    assert (
        demo_client.get("/api/me", headers=ben).json()["role"] == "support_specialist"
    )
    assert [
        item["id"] for item in demo_client.get("/api/tickets", headers=ben).json()
    ] == ["CHG-1043"]
    assert demo_client.get("/api/tickets/CHG-1042", headers=ben).status_code == 404
    assert demo_client.get("/api/approvals", headers=ben).json() == []
    assert {item["id"] for item in demo_client.get("/api/demo/personas").json()} == {
        "emp-alex",
        "emp-priya",
        "emp-ben",
    }
    assert "HttpOnly" in alex.headers["set-cookie"]
    assert "SameSite=lax" in alex.headers["set-cookie"]


@pytest.mark.parametrize("identity_mode", ["entra", "cli", "eval"])
def test_cookie_cannot_open_a_non_demo_workspace(
    demo_client, memory_store, identity_mode
):
    workspace_id = str(uuid4())
    payload = build_workspace_payload(
        scenario_id="baseline",
        selected_scenario=load_scenarios()["baseline"],
        identity_mode="eval",
    )
    payload["identityMode"] = identity_mode
    private_storage = memory_store.storage(workspace_id=workspace_id)
    private_storage.reset_workspace(payload=payload)
    original = private_storage.get_workspace()
    demo_client.cookies.set("switchboard-demo-workspace", workspace_id)
    response = demo_client.get("/api/me")
    assert response.status_code == 200
    assert response.headers["X-Switchboard-Workspace"] != workspace_id
    assert private_storage.get_workspace() == original


def test_profile_catalog_omits_accounts_outside_the_builtin_list(
    demo_client, memory_store
):
    workspace_id = str(uuid4())
    payload = build_workspace_payload(
        scenario_id="baseline",
        selected_scenario=load_scenarios()["baseline"],
        identity_mode="demo",
    )
    payload["records"]["employees"].append(
        {
            "id": "emp-real",
            "name": "Private employee",
            "active": True,
            "role": "technical_lead",
            "customer_ids": ["acme"],
        }
    )
    memory_store.storage(workspace_id=workspace_id).reset_workspace(payload=payload)
    demo_client.cookies.set("switchboard-demo-workspace", workspace_id)
    assert {item["id"] for item in demo_client.get("/api/demo/personas").json()} == {
        "emp-alex",
        "emp-priya",
        "emp-ben",
    }
    assert (
        demo_client.get(
            "/api/me", headers={"X-Demo-Persona-Id": "emp-real"}
        ).status_code
        == 403
    )
