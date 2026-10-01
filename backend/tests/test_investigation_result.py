"""Result consistency checks do not establish business authorization."""

import json
import re

import pytest
from pydantic import ValidationError

from switchboard.models import (
    InvestigationBlocker,
    InvestigationFindings,
    InvestigationResult,
)


@pytest.fixture
def candidate():
    return {
        "outcome": "proposal_candidate",
        "ticket_id": "CHG-1042",
        "proposed_endpoint": "https://events.acme.example/deals",
        "evidence_ids": ["CHG-1042", "acme", "int-acme-prod", "endpoint-change-v2"],
        "findings": {
            "overview": "The request supports preparing a proposal; approval is unverified.",
            "decision_criteria": [
                {
                    "name": "Requester authorization",
                    "status": "verified",
                    "required_before": "proposal",
                    "explanation": "The requester is registered for this customer.",
                    "policy_id": "endpoint-change-v2",
                    "evidence_ids": ["acme"],
                },
                {
                    "name": "Independent approval",
                    "status": "deferred",
                    "required_before": "execution",
                    "explanation": "A different technical lead must approve the proposal.",
                    "policy_id": "endpoint-change-v2",
                    "evidence_ids": [],
                },
            ],
            "recommendation": "Review the evidence before proceeding.",
        },
        "blockers": [],
    }


def test_valid_candidate(candidate):
    result = InvestigationResult.model_validate_json(json.dumps(candidate))

    assert result.ticket_id == "CHG-1042"
    assert result.outcome == "proposal_candidate"


def test_text_schema_supports_complete_ids_and_multiline_findings(candidate):
    # 1. Check the full-string pattern used by constrained model providers.
    schema = InvestigationResult.model_json_schema()
    ticket_pattern = schema["properties"]["ticket_id"]["anyOf"][0]["pattern"]
    overview_pattern = schema["$defs"]["InvestigationFindings"]["properties"][
        "overview"
    ]["pattern"]
    overview = "The request supports a proposal.\nApproval is still unverified."
    assert re.fullmatch(ticket_pattern, "CHG-1042")
    assert re.fullmatch(overview_pattern, overview)
    assert not re.fullmatch(overview_pattern, " \n\t ")

    # 2. Preserve the same validation when receiving the complete model output.
    candidate["findings"]["overview"] = overview
    result = InvestigationResult.model_validate_json(json.dumps(candidate))
    assert result.ticket_id == "CHG-1042"
    assert result.findings.overview == overview


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ticket_id", None, "requires a ticket ID"),
        ("proposed_endpoint", None, "requires a proposed endpoint"),
        ("evidence_ids", [], "requires supporting evidence"),
        (
            "blockers",
            [
                {
                    "kind": "confirmed_violation",
                    "summary": "Destination is not registered.",
                    "resolution": "Register the destination before trying again.",
                }
            ],
            "cannot have blockers",
        ),
    ],
)
def test_candidate_rejects_inconsistent_fields(candidate, field, value, message):
    candidate[field] = value

    with pytest.raises(ValidationError, match=message):
        InvestigationResult.model_validate_json(json.dumps(candidate))


def test_blocked_result_can_have_no_ticket_or_endpoint():
    # 1. Set up inputs and exercise the behavior under test.
    result = InvestigationResult(
        outcome="blocked",
        ticket_id=None,
        proposed_endpoint=None,
        evidence_ids=[],
        findings=InvestigationFindings(
            overview="The requested record is unavailable.",
            decision_criteria=[],
            recommendation="Review the evidence before proceeding.",
        ),
        blockers=[
            InvestigationBlocker(
                kind="missing_evidence",
                summary="Required evidence could not be retrieved.",
                resolution="Restore access to the record and investigate again.",
            )
        ],
    )

    # 2. Verify the expected result and any safety guarantees.
    assert result.ticket_id is None


def test_blocked_result_requires_a_reason(candidate):
    candidate["outcome"] = "blocked"

    with pytest.raises(ValidationError, match="requires at least one blocker"):
        InvestigationResult.model_validate_json(json.dumps(candidate))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticket_id", " "),
        ("proposed_endpoint", "not-a-url"),
        ("findings", {"overview": " "}),
        ("evidence_ids", [" "]),
        ("outcome", "approved"),
    ],
)
def test_invalid_field_values_are_rejected(candidate, field, value):
    candidate[field] = value

    with pytest.raises(ValidationError):
        InvestigationResult.model_validate_json(json.dumps(candidate))


@pytest.mark.parametrize("field", ["overview", "decision_criteria", "recommendation"])
def test_findings_sections_are_required(candidate, field):
    candidate["findings"].pop(field)
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate_json(json.dumps(candidate))


@pytest.mark.parametrize(
    "status", ["verified", "unverified", "unavailable", "failed", "deferred"]
)
def test_decision_criterion_accepts_each_agreed_status(candidate, status):
    candidate["findings"]["decision_criteria"][0]["status"] = status

    result = InvestigationResult.model_validate_json(json.dumps(candidate))

    assert result.findings.decision_criteria[0].status == status


def test_decision_criterion_rejects_an_unknown_status(candidate):
    candidate["findings"]["decision_criteria"][0]["status"] = "passed"

    with pytest.raises(ValidationError):
        InvestigationResult.model_validate_json(json.dumps(candidate))


def test_new_model_output_does_not_accept_legacy_summary(candidate):
    candidate.pop("findings")
    candidate["summary"] = "An old unstructured report."
    with pytest.raises(ValidationError):
        InvestigationResult.model_validate_json(json.dumps(candidate))
