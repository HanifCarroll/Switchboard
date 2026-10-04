"""Employee identity and demo workspace routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from switchboard.api.context import (
    RequestContext,
    get_request_context,
    require_demo_context,
)
from switchboard.demo.workspaces import DEMO_PERSONA_IDS
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.models import Role

router = APIRouter(prefix="/api")


class CurrentEmployee(BaseModel):
    employee_id: str
    name: str
    role: Role


@router.get("/me", response_model=CurrentEmployee)
def read_current_employee(
    context: RequestContext = Depends(get_request_context),
) -> CurrentEmployee:
    try:
        session = EmployeeSession(
            storage=context.storage, employee_id=context.employee_id
        )
        return CurrentEmployee(
            employee_id=session.employee_id,
            name=session.get_employee_name(employee_id=session.employee_id),
            role=session.get_active_employee_role(),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Employee unavailable") from None


class DemoPersona(BaseModel):
    id: str
    name: str
    role: Role


@router.get("/demo/personas", response_model=list[DemoPersona])
def read_demo_personas(
    context: RequestContext = Depends(require_demo_context),
) -> list[DemoPersona]:
    return [
        DemoPersona.model_validate(item)
        for item in context.storage.list_active_employees()
        if item["id"] in DEMO_PERSONA_IDS
    ]
