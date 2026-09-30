"""Persist proposals, approvals, and atomic endpoint-change executions."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from switchboard.integrations.customer_registry import get_customer
from switchboard.integrations.employee_directory import (
    CONFIG_ROLES,
    ROLES,
    EmployeeSession,
)
from switchboard.models import (
    Approval,
    ChangeWindow,
    DeliveryVerification,
    ExecuteProposalResult,
    Execution,
    Proposal,
    ProposalReviewResult,
    SaveProposalResult,
)
from switchboard.proposals import validate_endpoint_change_request
from switchboard.storage import StorageError


def _from_storage(model, record: dict):
    """Validate JSON-shaped stored values, including ISO datetime strings."""
    return model.model_validate_json(json.dumps(record))


def list_proposals_awaiting_approval(*, session: EmployeeSession) -> list[Proposal]:
    """Return pending proposals this employee may independently approve."""
    session.require_active_employee()

    proposals = []
    for record in session.storage.list_pending_proposals():
        proposal = _from_storage(Proposal, record)
        try:
            require_independent_proposal_reviewer(proposal=proposal, session=session)
        except (PermissionError, ValueError):
            continue
        proposals.append(proposal)
    return proposals


def save_proposal(
    *, proposal: Proposal, session: EmployeeSession
) -> SaveProposalResult:
    """Recheck authorization and records, then save or return an identical proposal."""
    # 1. Validate the snapshot against current business records.
    proposal = Proposal.model_validate_json(proposal.model_dump_json())
    ensure_proposal_matches_current_records(proposal=proposal, session=session)

    # 2. Let DynamoDB enforce one proposal for the same business snapshot.
    saved = session.storage.save_proposal(proposal=proposal.model_dump(mode="json"))
    return SaveProposalResult(
        proposal=_from_storage(Proposal, saved["proposal"]),
        was_created=saved["wasCreated"],
    )


def ensure_proposal_matches_current_records(
    *, proposal: Proposal, session: EmployeeSession
) -> None:
    """Raise if access, request validity, or the proposal snapshot has changed."""
    records = validate_endpoint_change_request(
        ticket_id=proposal.ticket_id,
        proposed_endpoint=proposal.proposed_endpoint,
        session=session,
    )
    ticket = records.ticket
    integration = records.integration

    if (
        proposal.proposed_by_employee_id != session.employee_id
        or proposal.requester_contact_id != ticket.requester_contact_id
        or proposal.customer_id != ticket.customer_id
        or proposal.integration_id != integration.id
        or proposal.environment != integration.environment
        or proposal.current_endpoint != integration.endpoint
        or proposal.expected_configuration_version != integration.version
    ):
        raise ValueError("Proposal no longer matches the employee or business records")


def get_proposal(*, session: EmployeeSession, proposal_id: str) -> Proposal:
    """Read a proposal for an active employee assigned to its customer."""
    try:
        session.require_active_employee()
        record = session.storage.get_proposal(proposal_id=proposal_id)
        if record is None:
            raise PermissionError("Record unavailable")
        proposal = _from_storage(Proposal, record)
        session.require_customer_access(
            customer_id=proposal.customer_id, allowed_roles=ROLES
        )
    except PermissionError:
        raise PermissionError("Record unavailable") from None
    return proposal


def approve_proposal(*, proposal_id: str, session: EmployeeSession) -> Approval:
    """Record independent technical-lead approval or return the existing approval."""
    # 1. Recheck proposal access and reviewer authority.
    proposal = get_proposal(session=session, proposal_id=proposal_id)
    require_independent_proposal_reviewer(proposal=proposal, session=session)

    # 2. Let DynamoDB enforce one approval per proposal.
    approval = Approval(
        id=str(uuid4()),
        proposal_id=proposal.id,
        approved_by_employee_id=session.employee_id,
        created_at=datetime.now(timezone.utc),
    )
    saved = session.storage.save_approval(approval=approval.model_dump(mode="json"))
    return _from_storage(Approval, saved)


def require_independent_proposal_reviewer(
    *, proposal: Proposal, session: EmployeeSession
) -> None:
    """Require the reviewer used by both the inbox and approval write."""
    session.require_customer_access(
        customer_id=proposal.customer_id,
        allowed_roles={"technical_lead"},
    )
    if session.employee_id == proposal.proposed_by_employee_id:
        raise PermissionError(
            "The proposing employee cannot approve their own proposal"
        )
    if proposal.environment != "production":
        raise ValueError("Sandbox proposals do not require independent approval")


def get_proposal_review(
    *, session: EmployeeSession, proposal_id: str
) -> ProposalReviewResult:
    """Return an accessible proposal and its optional approval and execution receipts."""
    proposal = get_proposal(session=session, proposal_id=proposal_id)
    approval_record = session.storage.get_approval(proposal_id=proposal_id)
    execution_record = session.storage.get_execution(proposal_id=proposal_id)
    verification_record = None
    if execution_record is not None:
        verification_record = session.storage.get_delivery_verification(
            execution_id=execution_record["id"]
        )
    return ProposalReviewResult(
        proposal=proposal,
        approval=_from_storage(Approval, approval_record) if approval_record else None,
        execution=_from_storage(Execution, execution_record)
        if execution_record
        else None,
        verification=_from_storage(DeliveryVerification, verification_record)
        if verification_record
        else None,
    )


def execute_proposal(
    *,
    proposal_id: str,
    session: EmployeeSession,
    executed_at: datetime,
) -> ExecuteProposalResult:
    """Execute a saved proposal or return its existing receipt on a retry."""
    if executed_at.utcoffset() is None:
        raise ValueError("Execution time must be timezone-aware")

    # 1. Recheck current executor access before reading a receipt.
    proposal = get_proposal(session=session, proposal_id=proposal_id)
    session.require_customer_access(
        customer_id=proposal.customer_id, allowed_roles=CONFIG_ROLES
    )
    receipt = session.storage.get_execution(proposal_id=proposal_id)
    if receipt is not None:
        return ExecuteProposalResult(
            execution=_from_storage(Execution, receipt), was_created=False
        )

    # 2. Recheck the proposer, request snapshot, approval, and change window.
    proposer = EmployeeSession(
        storage=session.storage,
        employee_id=proposal.proposed_by_employee_id,
    )
    ensure_proposal_matches_current_records(proposal=proposal, session=proposer)

    approval_id = None
    if proposal.environment == "production":
        approval = get_valid_production_approval(
            proposal=proposal,
            storage_session=session,
            executed_at=executed_at,
        )
        customer = get_customer(session=session, customer_id=proposal.customer_id)
        require_open_change_window(
            window=customer.production_change_window,
            executed_at=executed_at,
        )
        approval_id = approval.id

    # 3. Ask DynamoDB to change the configuration and save its receipt atomically.
    execution = Execution(
        id=str(uuid4()),
        proposal_id=proposal.id,
        executed_by_employee_id=session.employee_id,
        approval_id=approval_id,
        executed_at=executed_at,
        previous_configuration_version=proposal.expected_configuration_version,
        resulting_configuration_version=proposal.expected_configuration_version + 1,
    )
    try:
        applied = session.storage.apply_execution(
            execution=execution.model_dump(mode="json"),
            integration_id=proposal.integration_id,
            customer_id=proposal.customer_id,
            expected_version=proposal.expected_configuration_version,
            current_endpoint=str(proposal.current_endpoint),
            proposed_endpoint=str(proposal.proposed_endpoint),
        )
    except StorageError as error:
        if error.status == 409:
            raise ValueError(str(error)) from None
        raise

    return ExecuteProposalResult(
        execution=_from_storage(Execution, applied["execution"]),
        was_created=applied["wasCreated"],
    )


def get_valid_production_approval(
    *,
    proposal: Proposal,
    storage_session: EmployeeSession,
    executed_at: datetime,
) -> Approval:
    """Return current independent approval using the same workspace."""
    if proposal.environment != "production":
        raise ValueError("Sandbox proposals do not require independent approval")

    record = storage_session.storage.get_approval(proposal_id=proposal.id)
    if record is None:
        raise ValueError("Production proposal requires approval")
    approval = _from_storage(Approval, record)
    if approval.approved_by_employee_id == proposal.proposed_by_employee_id:
        raise PermissionError(
            "The proposing employee cannot approve their own proposal"
        )

    approver = EmployeeSession(
        storage=storage_session.storage,
        employee_id=approval.approved_by_employee_id,
    )
    approver.require_customer_access(
        customer_id=proposal.customer_id, allowed_roles={"technical_lead"}
    )
    if executed_at < approval.created_at:
        raise ValueError("Execution time precedes approval")
    return approval


def require_open_change_window(*, window: ChangeWindow, executed_at: datetime) -> None:
    """Raise when execution is outside the supported production change window."""
    if executed_at.utcoffset() is None:
        raise ValueError("Execution time must be timezone-aware")

    utc_time = executed_at.astimezone(timezone.utc)
    # ponytail: same-day UTC windows only; add overnight rules when needed.
    if not (
        window.start < window.end
        and utc_time.strftime("%A") == window.weekday
        and window.start <= utc_time.strftime("%H:%M") < window.end
    ):
        raise ValueError("Execution is outside the production change window")
