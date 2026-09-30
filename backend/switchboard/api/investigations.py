"""Ticket, investigation, and evidence routes."""

import json
import logging
import os
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from switchboard.api.context import RequestContext, get_request_context
from switchboard.integrations.support_desk import (
    get_ticket,
    get_ticket_details,
    list_tickets,
)
from switchboard.investigation.agent import create_model
from switchboard.investigation.evidence import read_investigation_evidence
from switchboard.investigation.fixtures import investigate_ticket_fixture
from switchboard.investigation.mode import get_investigation_mode
from switchboard.investigation.report_validation import ReportValidationError
from switchboard.investigation.runner import investigate_ticket
from switchboard.investigation.runs import (
    InvestigationRun,
    InvestigationSummary,
    get_investigation_run,
    list_investigation_runs,
)
from switchboard.investigation.tools import InvestigationContext, employee_session
from switchboard.jobs import InvestigationJob, history, read, submit
from switchboard.models import (
    EndpointChangeResult,
    InvestigationEvidenceDetail,
    Ticket,
    TicketDetails,
)
from switchboard.workflow_status import (
    WorkflowStatus,
    get_workflow_status,
    ready_to_investigate_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class InvestigationRequest(BaseModel):
    ticket_id: str
    idempotency_key: UUID | None = None


class AssignedRequestSummary(TicketDetails):
    workflow_status: WorkflowStatus
    needs_attention: bool


def _ticket_context(*, request_context: RequestContext) -> InvestigationContext:
    return InvestigationContext(
        storage=request_context.storage,
        employee_id=request_context.employee_id,
    )


def _get_accessible_ticket(
    *, ticket_id: str, request_context: RequestContext
) -> Ticket:
    try:
        with employee_session(
            _ticket_context(request_context=request_context)
        ) as session:
            ticket = get_ticket(session=session, ticket_id=ticket_id)
            if ticket.assigned_employee_id != request_context.employee_id:
                raise PermissionError("Ticket unavailable")
            return ticket
    except PermissionError:
        raise HTTPException(status_code=404, detail="Ticket unavailable") from None


def _request_summary(
    *, ticket: Ticket, request_context: RequestContext
) -> AssignedRequestSummary:
    context = _ticket_context(request_context=request_context)
    runs = request_context.storage.list_runs(ticket_id=ticket.id)
    if runs:
        result = EndpointChangeResult.model_validate_json(json.dumps(runs[0]["result"]))
        status = get_workflow_status(result=result, context=context)
    else:
        status = ready_to_investigate_status()
    with employee_session(context) as session:
        details = get_ticket_details(session=session, ticket=ticket)
    return AssignedRequestSummary(
        **details.model_dump(),
        workflow_status=status,
        needs_attention=status.code not in {"awaiting_approval", "delivery_verified"},
    )


@router.get("/tickets", response_model=list[AssignedRequestSummary])
def read_tickets(
    request_context: RequestContext = Depends(get_request_context),
) -> list[AssignedRequestSummary]:
    try:
        with employee_session(
            _ticket_context(request_context=request_context)
        ) as session:
            tickets = list_tickets(session=session)
        return [
            _request_summary(ticket=ticket, request_context=request_context)
            for ticket in tickets
        ]
    except PermissionError:
        raise HTTPException(status_code=403, detail="Employee unavailable") from None


@router.get("/tickets/{ticket_id}", response_model=TicketDetails)
def read_ticket(
    ticket_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> TicketDetails:
    try:
        ticket = _get_accessible_ticket(
            ticket_id=ticket_id,
            request_context=request_context,
        )
        with employee_session(
            _ticket_context(request_context=request_context)
        ) as session:
            return get_ticket_details(session=session, ticket=ticket)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Ticket unavailable") from None


@router.post("/investigations", response_model=InvestigationRun | InvestigationJob)
def start_investigation(
    request: InvestigationRequest,
    response: Response,
    request_context: RequestContext = Depends(get_request_context),
) -> InvestigationRun | InvestigationJob:
    _get_accessible_ticket(ticket_id=request.ticket_id, request_context=request_context)
    if os.getenv("INVESTIGATION_QUEUE_URL"):
        if request.idempotency_key is None:
            raise HTTPException(status_code=422, detail="Idempotency key required")
        job = submit(
            storage=request_context.storage,
            employee_id=request_context.employee_id,
            ticket_id=request.ticket_id,
            request_key=request.idempotency_key,
        )
        response.status_code = 202
        return job
    try:
        if get_investigation_mode() == "fixture":
            run = investigate_ticket_fixture(
                ticket_id=request.ticket_id,
                employee_id=request_context.employee_id,
                storage=request_context.storage,
                now=datetime.now(timezone.utc),
            )
        else:
            run = investigate_ticket(
                ticket_id=request.ticket_id,
                employee_id=request_context.employee_id,
                model=create_model(),
                storage=request_context.storage,
                now=datetime.now(timezone.utc),
            )
    except ReportValidationError:
        logger.exception("Investigation report validation failed")
        raise HTTPException(
            status_code=502,
            detail="The investigation report could not be validated. Try again.",
        ) from None
    except (ValueError, PermissionError):
        raise HTTPException(
            status_code=422, detail="Investigation result was rejected"
        ) from None
    except Exception:
        logger.exception("Investigation failed")
        raise HTTPException(
            status_code=502,
            detail="Investigation could not complete. Check run history before retrying; a proposal may already have been saved.",
        ) from None

    result = run.result
    return InvestigationRun(
        run_id=UUID(run.workflow_id),
        ticket_id=request.ticket_id,
        result=result,
        current_status=get_workflow_status(
            result=result,
            context=InvestigationContext(
                storage=request_context.storage,
                employee_id=request_context.employee_id,
            ),
        ),
    )


@router.get("/investigations", response_model=list[InvestigationSummary])
def list_investigations(
    ticket_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> list[InvestigationSummary]:
    _get_accessible_ticket(ticket_id=ticket_id, request_context=request_context)
    completed = list_investigation_runs(
        storage=request_context.storage,
        employee_id=request_context.employee_id,
        ticket_id=ticket_id,
    )
    if os.getenv("INVESTIGATION_QUEUE_URL"):
        pending = history(
            storage=request_context.storage,
            employee_id=request_context.employee_id,
            ticket_id=ticket_id,
        )
        return [
            InvestigationSummary.model_validate(item) for item in pending
        ] + completed
    return completed


@router.get(
    "/investigations/{run_id}", response_model=InvestigationRun | InvestigationJob
)
def read_investigation(
    run_id: UUID,
    request_context: RequestContext = Depends(get_request_context),
) -> InvestigationRun | InvestigationJob:
    try:
        if os.getenv("INVESTIGATION_QUEUE_URL"):
            return read(
                storage=request_context.storage,
                employee_id=request_context.employee_id,
                run_id=run_id,
            )
        return get_investigation_run(
            storage=request_context.storage,
            run_id=run_id,
            employee_id=request_context.employee_id,
        )
    except (FileNotFoundError, PermissionError):
        raise HTTPException(
            status_code=404, detail="Investigation unavailable"
        ) from None


@router.get(
    "/investigations/{run_id}/evidence/{evidence_id}",
    response_model=InvestigationEvidenceDetail,
)
def read_evidence(
    run_id: UUID,
    evidence_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> InvestigationEvidenceDetail:
    try:
        return read_investigation_evidence(
            storage=request_context.storage,
            run_id=run_id,
            evidence_id=evidence_id,
            employee_id=request_context.employee_id,
        )
    except (FileNotFoundError, PermissionError):
        raise HTTPException(status_code=404, detail="Evidence unavailable") from None
