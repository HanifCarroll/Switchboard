"""Shared investigation runner for the CLI and web demo."""

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from switchboard.change_management import save_proposal
from switchboard.dynamodb import DynamoStore
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket
from switchboard.investigation.agent import build_agent
from switchboard.investigation.runs import save_investigation_run
from switchboard.investigation.tools import InvestigationContext
from switchboard.investigation.workflow import (
    EndpointChangeContext,
    endpoint_change_graph,
)
from switchboard.models import EndpointChangeResult, InvestigationRunResult
from switchboard.proposals import validate_proposal
from switchboard.storage import WorkspaceStorage


def investigate_scenario(
    *,
    scenario_id: str,
    model: BaseChatModel,
    storage: WorkspaceStorage,
    employee_id: str | None = None,
) -> InvestigationRunResult:
    """Investigate the active synthetic scenario and retain its historical result."""
    setup = storage.get_workspace()
    if setup is None:
        raise ValueError("Reset the demo to a scenario before investigating")
    if setup["scenarioId"] != scenario_id:
        raise ValueError("Selected scenario is not active; reset the demo first")

    inputs = setup["inputs"]
    return _run_investigation(
        ticket_id=inputs["ticket_id"],
        request=inputs["request"],
        employee_id=employee_id or inputs["requester_employee_id"],
        model=model,
        storage=storage,
        now=inputs["now"],
        scenario_id=scenario_id,
    )


def investigate_ticket(
    *,
    ticket_id: str,
    employee_id: str,
    model: BaseChatModel,
    storage: WorkspaceStorage,
    now: datetime,
    run_id: UUID | None = None,
) -> InvestigationRunResult:
    """Investigate one accessible ticket using the current server time."""
    if now.utcoffset() is None:
        raise ValueError("Investigation time must be timezone-aware")
    return _run_investigation(
        ticket_id=ticket_id,
        request=f"Investigate endpoint change request in ticket {ticket_id}.",
        employee_id=employee_id,
        model=model,
        storage=storage,
        now=now.isoformat(),
        run_id=run_id,
    )


def _run_investigation(
    *,
    ticket_id: str,
    request: str,
    employee_id: str,
    model: BaseChatModel,
    storage: WorkspaceStorage,
    now: str,
    scenario_id: str | None = None,
    run_id: UUID | None = None,
) -> InvestigationRunResult:
    """Authorize, run, and persist one ticket investigation."""
    # 1. Authorize the selected ticket before invoking the model.
    session = EmployeeSession(storage=storage, employee_id=employee_id)
    ticket = get_ticket(session=session, ticket_id=ticket_id)
    role = session.get_active_employee_role()

    # 2. Run the access-controlled workflow with trusted context.
    workflow_id = run_id or uuid4()
    context = EndpointChangeContext(
        agent=build_agent(model=model, now=now),
        model=model,
        captured_at=datetime.fromisoformat(now),
        investigation_context=InvestigationContext(
            storage=storage,
            employee_id=employee_id,
        ),
    )
    cached = (
        storage.transport.load_checkpoint(storage.workspace_id)
        if isinstance(storage.transport, DynamoStore)
        else None
    )
    if cached:
        result = EndpointChangeResult.model_validate_json(json.dumps(cached))
        if result.investigation.outcome == "proposal_candidate":
            saved = save_proposal(
                proposal=validate_proposal(
                    investigation=result.investigation, session=session
                ),
                session=session,
            )
            result = result.model_copy(
                update={"proposal": saved.proposal, "was_created": saved.was_created}
            )
    else:
        raw_result = endpoint_change_graph.invoke(
            {"request": request, "ticket_id": ticket.id},
            context=context,
            config={
                "run_name": f"investigation-{scenario_id or ticket.id}",
                "metadata": {
                    "ticket_id": ticket.id,
                    "workflow_id": str(workflow_id),
                    **({"scenario_id": scenario_id} if scenario_id is not None else {}),
                },
            },
        )
        result = EndpointChangeResult.model_validate(raw_result)

    # 3. Persist the result and access snapshot in the workspace store.
    save_investigation_run(
        storage=storage,
        run_id=UUID(str(workflow_id)),
        ticket_id=ticket.id,
        scenario_id=scenario_id,
        requester_employee_id=employee_id,
        requester_role=role,
        customer_ids=[ticket.customer_id],
        result=result,
        created_at=datetime.now(timezone.utc),
    )
    return InvestigationRunResult(workflow_id=str(workflow_id), result=result)
