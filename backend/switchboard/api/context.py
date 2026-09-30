"""Request identity and workspace resolution."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response

from switchboard.auth import get_auth_mode, get_entra_employee_id
from switchboard.demo.scenarios import initialize_demo_workspace, load_scenarios
from switchboard.demo.workspaces import open_demo_workspace
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.storage import WorkspaceStorage

DEMO_WORKSPACE_COOKIE = "switchboard-demo-workspace"


@dataclass(frozen=True)
class RequestContext:
    identity_mode: Literal["demo", "entra"]
    employee_id: str
    workspace_id: str
    storage: WorkspaceStorage


def get_request_context(request: Request, response: Response) -> RequestContext:
    """Resolve trusted identity and isolated storage once per request."""
    auth_mode = get_auth_mode()
    has_authorization = "Authorization" in request.headers

    # 1. Bind verified Entra identity to its own durable workspace.
    if auth_mode == "entra" or (auth_mode == "hybrid" and has_authorization):
        employee_id = get_entra_employee_id(request)
        workspace_id = f"entra-{employee_id}"
        storage = WorkspaceStorage.from_environment(workspace_id=workspace_id)
        initialize_demo_workspace(
            storage=storage,
            scenario_id="baseline",
            selected_scenario=load_scenarios()["baseline"],
            identity_mode="entra",
        )
        response.headers["X-Switchboard-Workspace"] = workspace_id
        return RequestContext(
            identity_mode="entra",
            employee_id=employee_id,
            workspace_id=workspace_id,
            storage=storage,
        )

    if has_authorization:
        raise HTTPException(status_code=400, detail="Microsoft sign-in is disabled")

    # 2. Restore or create the anonymous visitor's isolated demo workspace.
    try:
        workspace_id = UUID(request.cookies[DEMO_WORKSPACE_COOKIE])
    except (KeyError, ValueError):
        workspace_id = None
    base_storage = WorkspaceStorage.from_environment(workspace_id="new-demo")
    opened = open_demo_workspace(
        workspace_id=workspace_id,
        base_storage=base_storage,
    )
    if opened.was_created:
        response.set_cookie(
            key=DEMO_WORKSPACE_COOKIE,
            value=str(opened.workspace.id),
            max_age=24 * 60 * 60,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
        )

    employee_id = request.headers.get("X-Demo-Persona-Id", "emp-alex")
    response.headers["X-Switchboard-Workspace"] = str(opened.workspace.id)
    try:
        EmployeeSession(
            storage=opened.workspace.storage,
            employee_id=employee_id,
        ).require_active_employee()
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="Demo persona unavailable"
        ) from None

    return RequestContext(
        identity_mode="demo",
        employee_id=employee_id,
        workspace_id=str(opened.workspace.id),
        storage=opened.workspace.storage,
    )


def require_demo_context(
    context: RequestContext = Depends(get_request_context),
) -> RequestContext:
    if context.identity_mode != "demo":
        raise HTTPException(status_code=403, detail="Demo features are disabled")
    return context
