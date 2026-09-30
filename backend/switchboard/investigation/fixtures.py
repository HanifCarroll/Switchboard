"""Deterministic local investigation responses using authorized business records."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import HttpUrl

from switchboard.change_management import save_proposal
from switchboard.integrations.configuration_service import get_integration
from switchboard.integrations.customer_registry import get_customer
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.policy_library import list_policies
from switchboard.integrations.support_desk import get_ticket
from switchboard.investigation.runs import save_investigation_run
from switchboard.models import (
    DecisionCriterion,
    EndpointChangeResult,
    EvidenceSnapshot,
    InvestigationBlocker,
    InvestigationFindings,
    InvestigationResult,
    InvestigationRunResult,
    PolicyDocument,
    ReportValidation,
)
from switchboard.proposals import validate_proposal
from switchboard.storage import WorkspaceStorage


def investigate_ticket_fixture(
    *,
    ticket_id: str,
    employee_id: str,
    storage: WorkspaceStorage,
    now: datetime,
    run_id: UUID | None = None,
) -> InvestigationRunResult:
    """Create an immediate fixture result through real access and persistence rules."""
    if now.utcoffset() is None:
        raise ValueError("Investigation time must be timezone-aware")

    # 1. Read the same authorized records available to the model-backed workflow.
    session = EmployeeSession(storage=storage, employee_id=employee_id)
    ticket = get_ticket(session=session, ticket_id=ticket_id)
    try:
        customer = get_customer(session=session, customer_id=ticket.customer_id)
        integration = get_integration(
            session=session, integration_id=ticket.integration_id
        )
    except PermissionError:
        customer = None
        integration = None
    policies = [PolicyDocument.model_validate(item) for item in list_policies(session)]

    # 2. Build a deterministic report for the current request.
    destination_registered = (
        any(
            destination.environment == integration.environment
            and destination.url == ticket.requested_endpoint
            for destination in customer.registered_destinations
        )
        if customer is not None and integration is not None
        else None
    )
    requester_authorized = (
        ticket.requester_contact_id
        in {contact.id for contact in customer.authorized_contacts}
        if customer is not None
        else None
    )
    investigation = _fixture_report(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        integration_id=ticket.integration_id,
        proposed_endpoint=ticket.requested_endpoint,
        requester_authorized=requester_authorized,
        destination_registered=destination_registered,
    )

    # 3. Use the ordinary proposal checks and durable write for supported requests.
    proposal = None
    was_created = None
    if investigation.outcome == "proposal_candidate":
        saved = save_proposal(
            proposal=validate_proposal(investigation=investigation, session=session),
            session=session,
        )
        proposal = saved.proposal
        was_created = saved.was_created

    # 4. Save the exact records represented by the fixture response.
    evidence = [
        EvidenceSnapshot(
            id=ticket.id,
            kind="ticket",
            captured_at=now,
            document=ticket,
        ),
        *[
            EvidenceSnapshot(
                id=policy.id,
                kind="policy",
                captured_at=now,
                document=policy,
            )
            for policy in policies
        ],
    ]
    if customer is not None and integration is not None:
        evidence[1:1] = [
            EvidenceSnapshot(
                id=customer.id,
                kind="customer",
                captured_at=now,
                document=customer,
            ),
            EvidenceSnapshot(
                id=integration.id,
                kind="integration",
                captured_at=now,
                document=integration,
            ),
        ]
    result = EndpointChangeResult(
        source="fixture",
        investigation=investigation,
        report_validation=ReportValidation(
            policy_ids=[policy.id for policy in policies],
            evaluation_count=0,
            revision_count=0,
        ),
        evidence=evidence,
        messages=[],
        proposal=proposal,
        was_created=was_created,
    )
    workflow_id = run_id or uuid4()
    save_investigation_run(
        storage=storage,
        run_id=workflow_id,
        ticket_id=ticket.id,
        scenario_id="local-fixture",
        requester_employee_id=employee_id,
        requester_role=session.get_active_employee_role(),
        customer_ids=[ticket.customer_id],
        result=result,
        created_at=datetime.now(timezone.utc),
    )
    return InvestigationRunResult(workflow_id=str(workflow_id), result=result)


def _fixture_report(
    *,
    ticket_id: str,
    customer_id: str,
    integration_id: str,
    proposed_endpoint: HttpUrl,
    requester_authorized: bool | None,
    destination_registered: bool | None,
) -> InvestigationResult:
    requester_status = (
        "unavailable"
        if requester_authorized is None
        else "verified"
        if requester_authorized
        else "failed"
    )
    destination_status = (
        "unavailable"
        if destination_registered is None
        else "verified"
        if destination_registered
        else "failed"
    )
    criteria = [
        DecisionCriterion(
            name="Requester authorization",
            status=requester_status,
            required_before="proposal",
            explanation=(
                "The customer record is unavailable to this employee."
                if requester_authorized is None
                else "The requester is an authorized customer contact."
                if requester_authorized
                else "The requester is not an authorized customer contact."
            ),
            policy_id="endpoint-change-v2",
            evidence_ids=[ticket_id],
        ),
        DecisionCriterion(
            name="Destination registration",
            status=destination_status,
            required_before="proposal",
            explanation=(
                "The integration record is unavailable to this employee."
                if destination_registered is None
                else "The requested destination is registered for this environment."
                if destination_registered
                else "The requested destination is not registered for this environment."
            ),
            policy_id="endpoint-change-v2",
            evidence_ids=[integration_id],
        ),
        DecisionCriterion(
            name="Independent approval",
            status="deferred",
            required_before="execution",
            explanation="A different technical lead must approve before execution.",
            policy_id="endpoint-change-v2",
            evidence_ids=[],
        ),
    ]
    blockers = []
    if requester_authorized is not True:
        blockers.append(
            InvestigationBlocker(
                kind=(
                    "missing_evidence"
                    if requester_authorized is None
                    else "confirmed_violation"
                ),
                summary=(
                    "Requester authorization could not be verified."
                    if requester_authorized is None
                    else "Requester authorization is missing."
                ),
                resolution=(
                    "Use an employee who can access the customer record."
                    if requester_authorized is None
                    else "Use an authorized customer contact for the request."
                ),
            )
        )
    if destination_registered is not True:
        blockers.append(
            InvestigationBlocker(
                kind=(
                    "missing_evidence"
                    if destination_registered is None
                    else "confirmed_violation"
                ),
                summary=(
                    "Destination registration could not be verified."
                    if destination_registered is None
                    else "The destination is not registered."
                ),
                resolution=(
                    "Use an employee who can access the integration record."
                    if destination_registered is None
                    else "Register the destination before proposing the change."
                ),
            )
        )

    if blockers:
        return InvestigationResult(
            outcome="blocked",
            ticket_id=ticket_id,
            proposed_endpoint=proposed_endpoint,
            evidence_ids=[ticket_id, customer_id, integration_id, "endpoint-change-v2"],
            findings=InvestigationFindings(
                overview="The request is blocked by current business records.",
                decision_criteria=criteria,
                recommendation="Resolve the failed requirements before preparing a proposal.",
            ),
            blockers=blockers,
        )

    return InvestigationResult(
        outcome="proposal_candidate",
        ticket_id=ticket_id,
        proposed_endpoint=proposed_endpoint,
        evidence_ids=[ticket_id, customer_id, integration_id, "endpoint-change-v2"],
        findings=InvestigationFindings(
            overview="The request is supported by current customer and integration records.",
            decision_criteria=criteria,
            recommendation="Review the saved proposal before execution.",
        ),
        blockers=[],
    )
