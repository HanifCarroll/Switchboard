"""Read saved investigations and enforce current access independently of HTTP."""

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from switchboard.investigation.tools import InvestigationContext, employee_session
from switchboard.models import EndpointChangeResult
from switchboard.storage import WorkspaceStorage
from switchboard.workflow_status import WorkflowStatus, get_workflow_status


class InvestigationRun(BaseModel):
    status: Literal["completed"] = "completed"
    run_id: UUID
    ticket_id: str
    scenario_id: str | None = None
    result: EndpointChangeResult
    current_status: WorkflowStatus


class InvestigationSummary(BaseModel):
    run_id: UUID
    ticket_id: str
    scenario_id: str | None = None
    outcome: Literal["proposal_candidate", "blocked", "queued", "running", "failed"]


def find_proposal_run_id(*, storage: WorkspaceStorage, proposal_id: str) -> UUID | None:
    run_id = storage.find_proposal_run(proposal_id=proposal_id)
    return UUID(run_id) if run_id else None


def load_run(*, storage: WorkspaceStorage, run_id: UUID) -> dict:
    run = storage.get_run(run_id=str(run_id))
    if run is None:
        raise FileNotFoundError("Investigation unavailable")
    return run


def require_run_access(*, context: InvestigationContext, run: dict) -> None:
    """Require the original requester and their current role and customer access."""
    if run["requester_employee_id"] != context.employee_id:
        raise PermissionError("Investigation unavailable")

    with employee_session(context) as session:
        if session.get_active_employee_role() != run["requester_role"]:
            raise PermissionError("Investigation unavailable")
        for customer_id in run["customer_ids"]:
            session.require_customer_access(
                customer_id=customer_id,
                allowed_roles={run["requester_role"]},
            )


def get_investigation_run(
    *, storage: WorkspaceStorage, run_id: UUID, employee_id: str
) -> InvestigationRun:
    """Restrict investigation history to its requester with current record access."""
    run = load_run(storage=storage, run_id=run_id)
    context = InvestigationContext(storage=storage, employee_id=employee_id)
    require_run_access(context=context, run=run)
    result = EndpointChangeResult.model_validate_json(json.dumps(run["result"]))
    return InvestigationRun(
        run_id=run_id,
        ticket_id=run["ticket_id"],
        scenario_id=run["scenario_id"],
        result=result,
        current_status=get_workflow_status(result=result, context=context),
    )


def list_investigation_runs(
    *, storage: WorkspaceStorage, employee_id: str, ticket_id: str
) -> list[InvestigationSummary]:
    runs = []
    for record in storage.list_runs(ticket_id=ticket_id):
        try:
            require_run_access(
                context=InvestigationContext(storage=storage, employee_id=employee_id),
                run=record,
            )
        except PermissionError:
            continue
        result = EndpointChangeResult.model_validate_json(json.dumps(record["result"]))
        runs.append(
            InvestigationSummary(
                run_id=UUID(record["id"]),
                ticket_id=record["ticket_id"],
                scenario_id=record["scenario_id"],
                outcome=result.investigation.outcome,
            )
        )
    return runs


def save_investigation_run(
    *,
    storage: WorkspaceStorage,
    run_id: UUID,
    ticket_id: str,
    scenario_id: str | None,
    requester_employee_id: str,
    requester_role: str,
    customer_ids: list[str],
    result: EndpointChangeResult,
    created_at: datetime,
) -> None:
    storage.save_run(
        run={
            "id": str(run_id),
            "ticket_id": ticket_id,
            "scenario_id": scenario_id,
            "requester_employee_id": requester_employee_id,
            "requester_role": requester_role,
            "customer_ids": customer_ids,
            "result": result.model_dump(mode="json"),
            "created_at": created_at.isoformat(),
        }
    )
