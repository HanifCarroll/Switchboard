"""Proposal review, approval, execution, and verification routes."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from switchboard.api.context import RequestContext, get_request_context
from switchboard.change_management import (
    approve_proposal,
    execute_proposal,
    get_proposal_review,
    list_proposals_awaiting_approval,
)
from switchboard.delivery_verification import verify_execution_delivery
from switchboard.investigation.runs import find_proposal_run_id, load_run
from switchboard.investigation.tools import InvestigationContext, employee_session
from switchboard.models import (
    Approval,
    DeliveryVerification,
    ExecuteProposalResult,
    Execution,
    Proposal,
    VerifyDeliveryResult,
)
from switchboard.workflow import signal
from switchboard.workflow_status import WorkflowStatus, proposal_status

router = APIRouter(prefix="/api")


class ProposalReview(BaseModel):
    proposal: Proposal
    approval: Approval | None
    execution: Execution | None
    verification: DeliveryVerification | None
    current_status: WorkflowStatus


class ApprovalInboxItem(BaseModel):
    run_id: UUID
    proposal: Proposal


def review_context(
    *, run_id: UUID, request_context: RequestContext
) -> InvestigationContext:
    """Resolve only a run inside the request's already isolated workspace."""
    try:
        load_run(storage=request_context.storage, run_id=run_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail="Scenario run unavailable"
        ) from None
    return InvestigationContext(
        storage=request_context.storage,
        employee_id=request_context.employee_id,
    )


@router.get("/approvals", response_model=list[ApprovalInboxItem])
def list_pending_approvals(
    request_context: RequestContext = Depends(get_request_context),
) -> list[ApprovalInboxItem]:
    context = InvestigationContext(
        storage=request_context.storage,
        employee_id=request_context.employee_id,
    )
    try:
        with employee_session(context) as session:
            proposals = list_proposals_awaiting_approval(session=session)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Employee unavailable") from None

    items = []
    for proposal in proposals:
        run_id = find_proposal_run_id(
            storage=request_context.storage,
            proposal_id=proposal.id,
        )
        if run_id is not None:
            items.append(ApprovalInboxItem(run_id=run_id, proposal=proposal))
    return items


@router.get("/runs/{run_id}/proposals/{proposal_id}", response_model=ProposalReview)
def read_proposal(
    run_id: UUID,
    proposal_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> ProposalReview:
    context = review_context(run_id=run_id, request_context=request_context)
    try:
        with employee_session(context) as session:
            review = get_proposal_review(session=session, proposal_id=proposal_id)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Proposal unavailable") from None

    return ProposalReview(
        proposal=review.proposal,
        approval=review.approval,
        execution=review.execution,
        verification=review.verification,
        current_status=proposal_status(
            proposal=review.proposal,
            approval=review.approval,
            execution=review.execution,
            verification=review.verification,
        ),
    )


@router.post("/runs/{run_id}/proposals/{proposal_id}/approval", response_model=Approval)
def record_approval(
    run_id: UUID,
    proposal_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> Approval:
    context = review_context(run_id=run_id, request_context=request_context)
    try:
        with employee_session(context) as session:
            approval = approve_proposal(session=session, proposal_id=proposal_id)
            signal(request_context.storage, run_id, "approval")
            return approval
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="Approval not permitted or proposal unavailable"
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=409, detail="Proposal is not eligible for independent approval"
        ) from None


@router.post(
    "/runs/{run_id}/proposals/{proposal_id}/execution",
    response_model=ExecuteProposalResult,
)
def record_execution(
    run_id: UUID,
    proposal_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> ExecuteProposalResult:
    context = review_context(run_id=run_id, request_context=request_context)
    try:
        with employee_session(context) as session:
            result = execute_proposal(
                proposal_id=proposal_id,
                session=session,
                executed_at=datetime.now(timezone.utc),
            )
            signal(request_context.storage, run_id, "execution")
            return result
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="Execution not permitted or proposal unavailable"
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="Proposal does not meet current execution requirements",
        ) from None


@router.post(
    "/runs/{run_id}/proposals/{proposal_id}/verification",
    response_model=VerifyDeliveryResult,
)
def record_delivery_verification(
    run_id: UUID,
    proposal_id: str,
    request_context: RequestContext = Depends(get_request_context),
) -> VerifyDeliveryResult:
    context = review_context(run_id=run_id, request_context=request_context)
    try:
        with employee_session(context) as session:
            result = verify_execution_delivery(
                proposal_id=proposal_id,
                session=session,
                verified_at=datetime.now(timezone.utc),
            )
            signal(request_context.storage, run_id, "verification")
            return result
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="Verification not permitted or proposal unavailable",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="Execution does not meet current delivery-verification requirements",
        ) from None
