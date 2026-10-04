"""Request identity and workspace resolution."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response

from switchboard.auth import get_auth_mode
from switchboard.demo.workspaces import DEMO_PERSONA_IDS, open_demo_workspace
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.storage import WorkspaceStorage

DEMO_WORKSPACE_COOKIE = "switchboard-demo-workspace"


@dataclass(frozen=True)
class RequestContext:
    identity_mode: Literal["demo"]
    employee_id: str
    workspace_id: str
    storage: WorkspaceStorage


def get_request_context(request: Request, response: Response) -> RequestContext:
    """Resolve trusted identity and isolated storage once per request."""
    get_auth_mode()

    # 1. Accept only a built-in demo profile, without account credentials.
    if (
        "Authorization" in request.headers
        or "X-Switchboard-Authorization" in request.headers
    ):
        raise HTTPException(status_code=400, detail="Account sign-in is unavailable")

    employee_id = request.headers.get("X-Demo-Persona-Id", "emp-alex")
    if employee_id not in DEMO_PERSONA_IDS:
        raise HTTPException(status_code=403, detail="Demo persona unavailable")

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
