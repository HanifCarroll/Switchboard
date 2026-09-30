"""Automatic policy validation is a gate before investigation persistence."""

import json
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from switchboard.investigation.report_validation import (
    PolicySource,
    ReportValidationError,
    evaluate_policy_claims,
    validate_investigation_report,
)
from switchboard.models import (
    InvestigationBlocker,
    InvestigationFindings,
    InvestigationResult,
)


def draft_report() -> InvestigationResult:
    return InvestigationResult(
        outcome="blocked",
        ticket_id="CHG-1042",
        proposed_endpoint=None,
        evidence_ids=["CHG-1042", "endpoint-change-v2"],
        findings=InvestigationFindings(
            overview="The request is blocked because production rollback is never allowed.",
            decision_criteria=[],
            recommendation="Stop for manual intervention.",
        ),
        blockers=[
            InvestigationBlocker(
                kind="confirmed_violation",
                summary="The destination is not registered.",
                resolution="Register it before trying again.",
            )
        ],
    )


POLICIES = [
    PolicySource(
        id="endpoint-change-v2",
        content="Restore the old endpoint only when the approved recovery plan permits it.",
    )
]


def issue_response() -> str:
    return json.dumps(
        {
            "issues": [
                {
                    "claim": "production rollback is never allowed",
                    "policy_id": "endpoint-change-v2",
                    "policy_excerpt": (
                        "Restore the old endpoint only when the approved recovery plan permits it."
                    ),
                    "issue_kind": "invented_absolute_prohibition",
                    "explanation": "The policy permits rollback under stated conditions.",
                }
            ]
        }
    )


def test_clean_report_passes_without_revision():
    model = GenericFakeChatModel(messages=iter([AIMessage(content='{"issues": []}')]))
    draft = draft_report()

    validated = validate_investigation_report(
        draft=draft, policies=POLICIES, model=model
    )

    assert validated.investigation == draft
    assert validated.validation.evaluation_count == 1
    assert validated.validation.revision_count == 0
    assert validated.validation.policy_ids == ["endpoint-change-v2"]


def test_policy_review_uses_reasoning_and_json_output():
    model = Mock(spec=ChatDeepSeek)
    validation_model = model.bind.return_value
    validation_model.invoke.return_value = AIMessage(content='{"issues": []}')

    evaluate_policy_claims(
        investigation_output=draft_report().model_dump(mode="json"),
        policies=POLICIES,
        model=model,
    )

    model.bind.assert_called_once_with(
        extra_body={"thinking": {"type": "enabled"}},
        response_format={"type": "json_object"},
        reasoning_effort="low",
        max_tokens=8192,
    )


def test_policy_issue_is_revised_once_and_rechecked():
    revised_findings = {
        "overview": "The request is blocked because the destination is not registered.",
        "decision_criteria": [],
        "recommendation": "Register the destination before investigating again.",
    }
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(content=issue_response()),
                AIMessage(content=json.dumps(revised_findings)),
                AIMessage(content='{"issues": []}'),
            ]
        )
    )

    validated = validate_investigation_report(
        draft=draft_report(), policies=POLICIES, model=model
    )

    assert validated.investigation.findings.overview == revised_findings["overview"]
    assert validated.validation.evaluation_count == 2
    assert validated.validation.revision_count == 1


def test_report_is_rejected_when_revision_still_has_policy_issues():
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(content=issue_response()),
                AIMessage(content=draft_report().findings.model_dump_json()),
                AIMessage(content=issue_response()),
            ]
        )
    )

    with pytest.raises(ReportValidationError, match="still contains policy issues"):
        validate_investigation_report(
            draft=draft_report(), policies=POLICIES, model=model
        )


def test_report_rejects_fabricated_policy_excerpt():
    fabricated = json.loads(issue_response())
    fabricated["issues"][0]["policy_excerpt"] = "This text is not in the policy."
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content=json.dumps(fabricated))])
    )

    with pytest.raises(ReportValidationError, match="unverified source excerpt"):
        validate_investigation_report(
            draft=draft_report(), policies=POLICIES, model=model
        )


@pytest.mark.parametrize(
    "response",
    [
        "Looks good",
        json.dumps(
            {
                "issues": [
                    {
                        "claim": "Never roll back.",
                        "policy_id": "endpoint-change-v2",
                        "policy_excerpt": POLICIES[0].content,
                        "issue_kind": "unclear_policy_problem",
                        "explanation": "Unsupported issue kind.",
                    }
                ]
            }
        ),
    ],
)
def test_invalid_judge_output_is_not_a_pass(response):
    model = GenericFakeChatModel(messages=iter([AIMessage(content=response)]))

    with pytest.raises(ReportValidationError, match="invalid output"):
        evaluate_policy_claims(
            investigation_output="Never roll back.",
            policies=POLICIES,
            model=model,
        )
