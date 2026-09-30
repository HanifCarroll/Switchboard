import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from langchain_core.messages import ToolMessage

from switchboard.integrations.customer_registry import get_customer
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket
from switchboard.investigation.evidence import (
    capture_investigation_evidence,
    read_investigation_evidence,
)
from switchboard.investigation.runs import save_investigation_run
from switchboard.models import (
    EndpointChangeResult,
    InvestigationBlocker,
    InvestigationFindings,
    InvestigationResult,
    ReportValidation,
)


def test_successful_tool_results_become_exact_evidence_snapshots(storage):
    session = EmployeeSession(storage=storage, employee_id="emp-alex")
    ticket = get_ticket(session=session, ticket_id="CHG-1042")
    customer = get_customer(session=session, customer_id="acme")
    captured_at = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)

    evidence = capture_investigation_evidence(
        messages=[
            ToolMessage(
                name="get_ticket",
                tool_call_id="ticket-call",
                content=ticket.model_dump_json(),
            ),
            ToolMessage(
                name="get_customer",
                tool_call_id="customer-call",
                content=customer.model_dump_json(),
            ),
            ToolMessage(
                name="get_integration",
                tool_call_id="failed-call",
                content="Record unavailable",
            ),
            ToolMessage(
                name="list_policies",
                tool_call_id="policy-call",
                content=json.dumps(storage.list_policies()),
            ),
        ],
        captured_at=captured_at,
    )

    assert [item.id for item in evidence] == [
        "CHG-1042",
        "acme",
        "endpoint-change-v1",
        "endpoint-change-v2",
    ]
    assert evidence[0].kind == "ticket"
    assert evidence[0].captured_at == captured_at
    assert evidence[0].document == ticket
    assert all(item.id != "int-acme-prod" for item in evidence)


def test_saved_evidence_preserves_the_snapshot_when_the_current_record_changes(
    storage, memory_store
):
    session = EmployeeSession(storage=storage, employee_id="emp-alex")
    integration = session.storage.get_integration(integration_id="int-acme-prod")
    captured_at = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    snapshot = capture_investigation_evidence(
        messages=[
            ToolMessage(
                name="get_integration",
                tool_call_id="integration-call",
                content=json.dumps(integration),
            )
        ],
        captured_at=captured_at,
    )[0]
    run_id = UUID("6a0fb861-b137-47b4-8360-c03e31b881f2")
    result = EndpointChangeResult(
        source="fixture",
        investigation=InvestigationResult(
            outcome="blocked",
            ticket_id="CHG-1042",
            proposed_endpoint=None,
            evidence_ids=["int-acme-prod"],
            findings=InvestigationFindings(
                overview="The request needs more evidence.",
                decision_criteria=[],
                recommendation="Review the missing evidence.",
            ),
            blockers=[
                InvestigationBlocker(
                    kind="missing_evidence",
                    summary="Evidence is incomplete.",
                    resolution="Investigate again.",
                )
            ],
        ),
        report_validation=ReportValidation(
            policy_ids=["endpoint-change-v2"],
            evaluation_count=1,
            revision_count=0,
        ),
        evidence=[snapshot],
        messages=[],
    )
    save_investigation_run(
        storage=storage,
        run_id=run_id,
        ticket_id="CHG-1042",
        scenario_id=None,
        requester_employee_id="emp-alex",
        requester_role="implementation_engineer",
        customer_ids=["acme"],
        result=result,
        created_at=captured_at,
    )
    storage.apply_execution(
        execution={
            "id": "execution-after-investigation",
            "proposal_id": "proposal-after-investigation",
            "executed_by_employee_id": "emp-alex",
            "approval_id": None,
            "executed_at": "2026-09-22T14:05:00+00:00",
            "previous_configuration_version": 7,
            "resulting_configuration_version": 8,
        },
        integration_id="int-acme-prod",
        customer_id="acme",
        expected_version=7,
        current_endpoint="https://old.acme.example/deals",
        proposed_endpoint="https://events.acme.example/deals",
    )

    detail = read_investigation_evidence(
        run_id=run_id,
        evidence_id="int-acme-prod",
        employee_id="emp-alex",
        storage=storage,
    )

    assert detail.snapshot.document == snapshot.document
    assert detail.current_document is not None
    assert detail.current_document != detail.snapshot.document
    assert detail.has_changed is True

    with pytest.raises(PermissionError, match="Investigation unavailable"):
        read_investigation_evidence(
            run_id=run_id,
            evidence_id="int-acme-prod",
            employee_id="emp-ben",
            storage=storage,
        )
    with pytest.raises(FileNotFoundError, match="Investigation unavailable"):
        read_investigation_evidence(
            run_id=run_id,
            evidence_id="int-acme-prod",
            employee_id="emp-alex",
            storage=memory_store.storage(workspace_id="workspace-two"),
        )
