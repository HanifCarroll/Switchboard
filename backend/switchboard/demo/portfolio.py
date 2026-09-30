"""Initialize one persistent demo workspace with independent workflow examples."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import HttpUrl

from switchboard.change_management import approve_proposal, save_proposal
from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.integrations.support_desk import get_ticket
from switchboard.models import (
    Customer,
    DecisionCriterion,
    EndpointChangeResult,
    Integration,
    InvestigationFindings,
    InvestigationResult,
    ReportValidation,
    Ticket,
)
from switchboard.proposals import validate_proposal
from switchboard.storage import WorkspaceStorage


def initialize_demo_portfolio(*, storage: WorkspaceStorage) -> None:
    """Create the full demo portfolio and remove it if seeding cannot complete."""
    payload = build_workspace_payload(
        scenario_id="baseline",
        selected_scenario=load_scenarios()["baseline"],
        identity_mode="demo",
    )
    payload["scenarioId"] = "portfolio"
    _add_portfolio_records(payload=payload)
    _open_current_change_window(payload=payload)

    try:
        with storage.initialization():
            storage.reset_workspace(payload=payload)
            _seed_proposal(storage=storage, ticket_id="CHG-1045", approve=False)
            _seed_proposal(storage=storage, ticket_id="CHG-1046", approve=True)
    except Exception:
        storage.discard_failed_initialization()
        raise


def _seed_proposal(*, storage: WorkspaceStorage, ticket_id: str, approve: bool) -> None:
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    ticket = get_ticket(session=author, ticket_id=ticket_id)
    investigation = InvestigationResult(
        outcome="proposal_candidate",
        ticket_id=ticket.id,
        proposed_endpoint=HttpUrl(str(ticket.requested_endpoint)),
        evidence_ids=[ticket.id, ticket.customer_id, ticket.integration_id],
        findings=InvestigationFindings(
            overview="Current records support preparing the requested change.",
            decision_criteria=[
                DecisionCriterion(
                    name="Requester and destination",
                    status="verified",
                    required_before="proposal",
                    explanation="The requester and destination are registered.",
                    policy_id="endpoint-change-v2",
                    evidence_ids=[ticket.customer_id, ticket.integration_id],
                ),
                DecisionCriterion(
                    name="Independent approval",
                    status="deferred",
                    required_before="execution",
                    explanation="A different technical lead must approve before execution.",
                    policy_id="endpoint-change-v2",
                    evidence_ids=[],
                ),
            ],
            recommendation="Review the saved proposal before execution.",
        ),
        blockers=[],
    )
    proposal = save_proposal(
        proposal=validate_proposal(investigation=investigation, session=author),
        session=author,
    ).proposal
    result = EndpointChangeResult(
        source="fixture",
        investigation=investigation,
        report_validation=ReportValidation(
            policy_ids=["endpoint-change-v1", "endpoint-change-v2"],
            evaluation_count=1,
            revision_count=0,
        ),
        evidence=[],
        messages=[],
        proposal=proposal,
        was_created=True,
    )
    storage.save_run(
        run={
            "id": str(uuid4()),
            "ticket_id": ticket.id,
            "scenario_id": "ready-to-execute" if approve else "pending-approval",
            "requester_employee_id": "emp-alex",
            "requester_role": "implementation_engineer",
            "customer_ids": [ticket.customer_id],
            "result": result.model_dump(mode="json"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if approve:
        reviewer = EmployeeSession(storage=storage, employee_id="emp-priya")
        approve_proposal(proposal_id=proposal.id, session=reviewer)


def _add_portfolio_records(*, payload: dict) -> None:
    records = payload["records"]
    ticket_records = [
        {
            "id": "CHG-1043",
            "customer_id": "globex",
            "integration_id": "int-globex-prod",
            "requester_contact_id": "contact-sam",
            "requester_name": "Sam Patel",
            "assigned_employee_id": "emp-ben",
            "created_at": "2026-09-22T13:35:00Z",
            "status": "open",
            "subject": "Route production CRM events to a new endpoint",
            "body": "Please move Globex production CRM events to https://new.globex.example/deals today. Keep all other settings unchanged. — Sam Patel",
            "requested_endpoint": "https://new.globex.example/deals",
        },
        {
            "id": "CHG-1044",
            "customer_id": "acme",
            "integration_id": "int-acme-billing-prod",
            "requester_contact_id": "contact-taylor",
            "requester_name": "Taylor Reed",
            "assigned_employee_id": "emp-alex",
            "created_at": "2026-09-22T13:40:00Z",
            "status": "open",
            "subject": "Update billing event delivery endpoint",
            "body": "Please move Acme billing events to https://events.acme.example/billing. I have authority to request this. Keep all other settings unchanged. — Taylor Reed",
            "requested_endpoint": "https://events.acme.example/billing",
        },
        {
            "id": "CHG-1045",
            "customer_id": "acme",
            "integration_id": "int-acme-orders-prod",
            "requester_contact_id": "contact-jordan",
            "requester_name": "Jordan Lee",
            "assigned_employee_id": "emp-alex",
            "created_at": "2026-09-22T13:45:00Z",
            "status": "open",
            "subject": "Update order event delivery endpoint",
            "body": "Please move Acme order events to https://events.acme.example/orders during today's approved change window. — Jordan Lee",
            "requested_endpoint": "https://events.acme.example/orders",
        },
        {
            "id": "CHG-1046",
            "customer_id": "acme",
            "integration_id": "int-acme-marketing-prod",
            "requester_contact_id": "contact-jordan",
            "requester_name": "Jordan Lee",
            "assigned_employee_id": "emp-alex",
            "created_at": "2026-09-22T13:50:00Z",
            "status": "open",
            "subject": "Update marketing event delivery endpoint",
            "body": "Please move Acme marketing events to https://events.acme.example/marketing during today's approved change window. — Jordan Lee",
            "requested_endpoint": "https://events.acme.example/marketing",
        },
    ]
    integration_records = [
        {
            "id": "int-acme-billing-prod",
            "customer_id": "acme",
            "name": "Billing events",
            "environment": "production",
            "endpoint": "https://old.acme.example/billing",
            "version": 3,
        },
        {
            "id": "int-acme-orders-prod",
            "customer_id": "acme",
            "name": "Order events",
            "environment": "production",
            "endpoint": "https://old.acme.example/orders",
            "version": 5,
        },
        {
            "id": "int-acme-marketing-prod",
            "customer_id": "acme",
            "name": "Marketing events",
            "environment": "production",
            "endpoint": "https://old.acme.example/marketing",
            "version": 2,
        },
    ]
    records["tickets"].extend(
        Ticket.model_validate_json(json.dumps(item)).model_dump(mode="json")
        for item in ticket_records
    )
    records["integrations"].extend(
        Integration.model_validate_json(json.dumps(item)).model_dump(mode="json")
        for item in integration_records
    )

    acme = next(item for item in records["customers"] if item["id"] == "acme")
    for endpoint in ("billing", "orders", "marketing"):
        acme["registered_destinations"].extend(
            [
                {
                    "environment": "production",
                    "url": f"https://old.acme.example/{endpoint}",
                },
                {
                    "environment": "production",
                    "url": f"https://events.acme.example/{endpoint}",
                },
            ]
        )
    Customer.model_validate_json(json.dumps(acme))


def _open_current_change_window(*, payload: dict) -> None:
    now = datetime.now(timezone.utc)
    customer = next(
        item for item in payload["records"]["customers"] if item["id"] == "acme"
    )
    customer["production_change_window"] = {
        "weekday": now.strftime("%A"),
        "start": "00:00",
        "end": "23:59",
        "timezone": "UTC",
    }
