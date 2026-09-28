from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl

from switchboard import change_management, proposals
from switchboard.change_management import (
    approve_proposal,
    execute_proposal,
    get_proposal,
    list_proposals_awaiting_approval,
    save_proposal,
)
from switchboard.delivery_verification import verify_execution_delivery
from switchboard.integrations import delivery_service
from switchboard.integrations.delivery_service import DeliveryTestResult
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket, list_tickets
from switchboard.investigation.runs import (
    get_investigation_run,
    list_investigation_runs,
    save_investigation_run,
)
from switchboard.models import (
    DecisionCriterion,
    EndpointChangeResult,
    InvestigationFindings,
    InvestigationResult,
    ReportValidation,
)
from switchboard.proposals import validate_proposal


@pytest.fixture(autouse=True)
def fixed_business_time(monkeypatch):
    """Create proposals and approvals before the scenario's execution time."""
    scenario_clock = Mock(wraps=datetime)
    scenario_clock.now.return_value = datetime(2026, 9, 22, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(change_management, "datetime", scenario_clock)
    monkeypatch.setattr(proposals, "datetime", scenario_clock)


def candidate() -> InvestigationResult:
    return InvestigationResult(
        outcome="proposal_candidate",
        ticket_id="CHG-1042",
        proposed_endpoint=HttpUrl("https://events.acme.example/deals"),
        evidence_ids=["CHG-1042", "acme", "int-acme-prod"],
        findings=InvestigationFindings(
            overview="Current records support the requested endpoint change.",
            decision_criteria=[
                DecisionCriterion(
                    name="Requester and destination",
                    status="verified",
                    required_before="proposal",
                    explanation="The requester and destination are registered.",
                    policy_id="endpoint-change-v2",
                    evidence_ids=["acme", "int-acme-prod"],
                ),
                DecisionCriterion(
                    name="Independent approval",
                    status="deferred",
                    required_before="execution",
                    explanation="A different technical lead must approve the proposal.",
                    policy_id="endpoint-change-v2",
                    evidence_ids=[],
                ),
            ],
            recommendation="Prepare the proposal for review.",
        ),
        blockers=[],
    )


def saved_proposal(storage):
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    proposal = validate_proposal(investigation=candidate(), session=author)
    return save_proposal(proposal=proposal, session=author).proposal


def test_customer_access_applies_to_single_and_collection_reads(storage):
    alex = EmployeeSession(storage=storage, employee_id="emp-alex")
    ben = EmployeeSession(storage=storage, employee_id="emp-ben")

    assert get_ticket(session=alex, ticket_id="CHG-1042").customer_id == "acme"
    assert [ticket.id for ticket in list_tickets(session=alex)] == ["CHG-1042"]
    assert list_tickets(session=ben) == []
    with pytest.raises(PermissionError, match="Record unavailable"):
        get_ticket(session=ben, ticket_id="CHG-1042")


def test_proposal_save_and_approval_retries_are_idempotent(storage):
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    proposal = validate_proposal(investigation=candidate(), session=author)

    first = save_proposal(proposal=proposal, session=author)
    second = save_proposal(
        proposal=proposal.model_copy(update={"id": str(uuid4())}), session=author
    )
    reviewer = EmployeeSession(storage=storage, employee_id="emp-priya")
    first_approval = approve_proposal(proposal_id=first.proposal.id, session=reviewer)
    retry_approval = approve_proposal(proposal_id=first.proposal.id, session=reviewer)

    assert first.was_created is True
    assert second.was_created is False
    assert second.proposal.id == first.proposal.id
    assert retry_approval.id == first_approval.id


def test_only_an_independent_technical_lead_can_approve(storage):
    proposal = saved_proposal(storage)

    with pytest.raises(PermissionError):
        approve_proposal(
            proposal_id=proposal.id,
            session=EmployeeSession(storage=storage, employee_id="emp-alex"),
        )
    with pytest.raises(PermissionError):
        approve_proposal(
            proposal_id=proposal.id,
            session=EmployeeSession(storage=storage, employee_id="emp-ben"),
        )

    reviewer = EmployeeSession(storage=storage, employee_id="emp-priya")
    assert [item.id for item in list_proposals_awaiting_approval(session=reviewer)] == [
        proposal.id
    ]


def test_execution_is_atomic_and_returns_the_same_receipt_on_retry(storage):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    executor = EmployeeSession(storage=storage, employee_id="emp-alex")
    executed_at = datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc)

    first = execute_proposal(
        proposal_id=proposal.id, session=executor, executed_at=executed_at
    )
    retry = execute_proposal(
        proposal_id=proposal.id, session=executor, executed_at=executed_at
    )

    integration = storage.get_integration(integration_id="int-acme-prod")
    assert first.was_created is True
    assert retry.was_created is False
    assert retry.execution.id == first.execution.id
    assert integration["endpoint"] == "https://events.acme.example/deals"
    assert integration["version"] == 8


def test_execution_before_approval_is_rejected_without_writes(storage):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )

    with pytest.raises(ValueError, match="Execution time precedes approval"):
        execute_proposal(
            proposal_id=proposal.id,
            session=EmployeeSession(storage=storage, employee_id="emp-alex"),
            executed_at=datetime(2026, 9, 22, 14, 9, tzinfo=timezone.utc),
        )

    assert storage.get_execution(proposal_id=proposal.id) is None
    assert storage.get_integration(integration_id="int-acme-prod")["version"] == 7


def test_successful_delivery_verification_closes_the_ticket(storage):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    executor = EmployeeSession(storage=storage, employee_id="emp-alex")
    execute_proposal(
        proposal_id=proposal.id,
        session=executor,
        executed_at=datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc),
    )

    result = verify_execution_delivery(
        proposal_id=proposal.id,
        session=executor,
        verified_at=datetime(2026, 9, 22, 14, 16, tzinfo=timezone.utc),
    )

    assert result.was_created is True
    assert result.verification.outcome == "delivered"
    assert storage.get_ticket(ticket_id="CHG-1042")["status"] == "closed"


def test_delivery_verification_retry_does_not_send_another_event(storage, monkeypatch):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    executor = EmployeeSession(storage=storage, employee_id="emp-alex")
    execute_proposal(
        proposal_id=proposal.id,
        session=executor,
        executed_at=datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc),
    )
    sent_event_ids = []

    def send_event(*, destination: str, test_event_id: str):
        sent_event_ids.append(test_event_id)
        return DeliveryTestResult(
            outcome="delivered",
            test_event_id=test_event_id,
            evidence=f"Accepted by {destination}",
        )

    monkeypatch.setattr(delivery_service, "send_synthetic_test_event", send_event)
    first = verify_execution_delivery(
        proposal_id=proposal.id,
        session=executor,
        verified_at=datetime(2026, 9, 22, 14, 16, tzinfo=timezone.utc),
    )
    retry = verify_execution_delivery(
        proposal_id=proposal.id,
        session=executor,
        verified_at=datetime(2026, 9, 22, 14, 17, tzinfo=timezone.utc),
    )

    assert first.was_created is True
    assert retry.was_created is False
    assert retry.verification.id == first.verification.id
    assert sent_event_ids == [first.verification.test_event_id]


@pytest.mark.parametrize("outcome", ["failed", "inconclusive"])
def test_unsuccessful_delivery_requires_manual_attention(storage, monkeypatch, outcome):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    executor = EmployeeSession(storage=storage, employee_id="emp-alex")
    execute_proposal(
        proposal_id=proposal.id,
        session=executor,
        executed_at=datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        delivery_service,
        "send_synthetic_test_event",
        lambda *, destination, test_event_id: DeliveryTestResult(
            outcome=outcome,
            test_event_id=test_event_id,
            evidence=f"No confirmed delivery from {destination}",
        ),
    )

    result = verify_execution_delivery(
        proposal_id=proposal.id,
        session=executor,
        verified_at=datetime(2026, 9, 22, 14, 16, tzinfo=timezone.utc),
    )

    assert result.verification.outcome == outcome
    assert storage.get_ticket(ticket_id="CHG-1042")["status"] == "needs_attention"


def test_delivery_verification_requires_execution_and_current_configuration(storage):
    proposal = saved_proposal(storage)
    executor = EmployeeSession(storage=storage, employee_id="emp-alex")
    verified_at = datetime(2026, 9, 22, 14, 16, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="before execution"):
        verify_execution_delivery(
            proposal_id=proposal.id,
            session=executor,
            verified_at=verified_at,
        )

    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    execution = execute_proposal(
        proposal_id=proposal.id,
        session=executor,
        executed_at=datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc),
    ).execution
    storage.apply_execution(
        execution={
            "id": "later-execution",
            "proposal_id": "later-proposal",
            "executed_by_employee_id": "emp-alex",
            "approval_id": None,
            "executed_at": "2026-09-22T14:15:30+00:00",
            "previous_configuration_version": execution.resulting_configuration_version,
            "resulting_configuration_version": execution.resulting_configuration_version
            + 1,
        },
        integration_id="int-acme-prod",
        customer_id="acme",
        expected_version=execution.resulting_configuration_version,
        current_endpoint="https://events.acme.example/deals",
        proposed_endpoint="https://later.acme.example/deals",
    )

    with pytest.raises(ValueError, match="changed after execution"):
        verify_execution_delivery(
            proposal_id=proposal.id,
            session=executor,
            verified_at=verified_at,
        )
    assert storage.get_delivery_verification(execution_id=execution.id) is None


def test_execution_rechecks_current_configuration_before_any_write(storage):
    proposal = saved_proposal(storage)
    approve_proposal(
        proposal_id=proposal.id,
        session=EmployeeSession(storage=storage, employee_id="emp-priya"),
    )
    storage.apply_execution(
        execution={
            "id": "other-execution",
            "proposal_id": "other-proposal",
            "executed_by_employee_id": "emp-alex",
            "approval_id": None,
            "executed_at": "2026-09-22T14:10:00+00:00",
            "previous_configuration_version": 7,
            "resulting_configuration_version": 8,
        },
        integration_id="int-acme-prod",
        customer_id="acme",
        expected_version=7,
        current_endpoint="https://old.acme.example/deals",
        proposed_endpoint="https://changed.example/deals",
    )

    with pytest.raises(ValueError, match="no longer matches"):
        execute_proposal(
            proposal_id=proposal.id,
            session=EmployeeSession(storage=storage, employee_id="emp-alex"),
            executed_at=datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc),
        )
    assert storage.get_execution(proposal_id=proposal.id) is None


def test_investigation_history_rechecks_original_employee_access(storage):
    proposal = saved_proposal(storage)
    run_id = UUID("8f2a6b6e-265f-4d54-a373-8a461285a831")
    result = EndpointChangeResult(
        source="fixture",
        investigation=candidate(),
        report_validation=ReportValidation(
            policy_ids=["endpoint-change-v2"],
            evaluation_count=1,
            revision_count=0,
        ),
        evidence=[],
        messages=[],
        proposal=proposal,
        was_created=True,
    )
    save_investigation_run(
        storage=storage,
        run_id=run_id,
        ticket_id="CHG-1042",
        scenario_id="baseline",
        requester_employee_id="emp-alex",
        requester_role="implementation_engineer",
        customer_ids=["acme"],
        result=result,
        created_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )

    saved_run = get_investigation_run(
        storage=storage, run_id=run_id, employee_id="emp-alex"
    )
    assert saved_run.result.proposal is not None
    assert saved_run.result.proposal.id == proposal.id
    assert (
        len(
            list_investigation_runs(
                storage=storage, employee_id="emp-alex", ticket_id="CHG-1042"
            )
        )
        == 1
    )
    with pytest.raises(PermissionError):
        get_investigation_run(storage=storage, run_id=run_id, employee_id="emp-priya")


def test_missing_and_inaccessible_proposals_share_the_same_error(storage):
    proposal = saved_proposal(storage)
    ben = EmployeeSession(storage=storage, employee_id="emp-ben")

    for proposal_id in (proposal.id, "missing"):
        with pytest.raises(PermissionError, match="Record unavailable"):
            get_proposal(session=ben, proposal_id=proposal_id)
