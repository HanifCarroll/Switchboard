"""Validate investigation wording against the policies used by the workspace."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from opentelemetry import trace
from pydantic import Field, ValidationError

from switchboard.investigation.agent import policy_review_model
from switchboard.investigation.deadline import require_time
from switchboard.models import (
    InvestigationFindings,
    InvestigationResult,
    PolicyDocument,
    Record,
    ReportValidation,
    Text,
)

PolicyIssueKind = Literal[
    "missing_required_condition",
    "invented_absolute_prohibition",
    "invented_requirement",
    "contradicts_policy",
]


PolicySource = PolicyDocument


class PolicyIssue(Record):
    claim: Text
    policy_id: Text
    policy_excerpt: Text
    explanation: Text
    issue_kind: PolicyIssueKind = Field(
        description=(
            "Classify the REPORT's claim, not the POLICY's instruction: "
            "missing_required_condition: REPORT allows X when a POLICY safeguard "
            "for X is unmet; invented_absolute_prohibition: REPORT forbids X that "
            "POLICY allows; invented_requirement: REPORT demands an additional "
            "step POLICY does not require; contradicts_policy: REPORT allows X "
            "that POLICY never allows, or another conflict not matching those kinds."
        )
    )


class PolicyReview(Record):
    issues: list[PolicyIssue]


class ReportValidationError(ValueError):
    """The report could not be shown as policy-faithful."""


@dataclass(frozen=True)
class ValidatedInvestigationReport:
    investigation: InvestigationResult
    validation: ReportValidation


def validate_investigation_report(
    *,
    draft: InvestigationResult,
    policies: list[PolicySource],
    model: BaseChatModel,
) -> ValidatedInvestigationReport:
    """Evaluate a report, revise its findings once if needed, and fail closed."""
    # 1. Require policy sources and evaluate the original report.
    if not policies:
        raise ReportValidationError("Report validation requires policy sources")

    first_review = evaluate_policy_claims(
        investigation_output=draft.model_dump(mode="json"),
        policies=policies,
        model=model,
    )
    if not first_review.issues:
        return ValidatedInvestigationReport(
            investigation=draft,
            validation=_validation_receipt(
                policies=policies,
                evaluation_count=1,
                revision_count=0,
            ),
        )

    # 2. Revise only the narrative findings, preserving the trusted outcome and facts.
    revised_findings = _revise_findings(
        draft=draft,
        review=first_review,
        policies=policies,
        model=model,
    )
    revised_report = draft.model_copy(update={"findings": revised_findings})

    # 3. Recheck once and reject a report that still changes policy meaning.
    second_review = evaluate_policy_claims(
        investigation_output=revised_report.model_dump(mode="json"),
        policies=policies,
        model=model,
    )
    if second_review.issues:
        raise ReportValidationError("Revised report still contains policy issues")

    return ValidatedInvestigationReport(
        investigation=revised_report,
        validation=_validation_receipt(
            policies=policies,
            evaluation_count=2,
            revision_count=1,
        ),
    )


@trace.get_tracer(__name__).start_as_current_span(
    "Policy review", record_exception=False, set_status_on_exception=False
)
def evaluate_policy_claims(
    *,
    investigation_output: str | dict,
    policies: list[PolicySource],
    model: BaseChatModel,
) -> PolicyReview:
    require_time()
    prompt = (Path(__file__).parent / "prompts" / "policy_evaluation.md").read_text()
    response = policy_review_model(model).invoke(
        [
            (
                "system",
                prompt
                + "\n\nOutput schema:\n"
                + json.dumps(PolicyReview.model_json_schema()),
            ),
            (
                "human",
                json.dumps(
                    {
                        "policies": [policy.model_dump() for policy in policies],
                        "investigation_output": investigation_output,
                    }
                ),
            ),
        ],
        config={"run_name": "policy-faithfulness-review"},
    )
    try:
        review = PolicyReview.model_validate_json(response.text)
    except ValidationError as error:
        raise ReportValidationError("Policy review returned invalid output") from error

    sources = {policy.id: policy.content for policy in policies}
    for issue in review.issues:
        source = sources.get(issue.policy_id)
        if source is None or issue.policy_excerpt not in source:
            raise ReportValidationError(
                "Policy review contains an unverified source excerpt"
            )

    return review


@trace.get_tracer(__name__).start_as_current_span(
    "Report revision", record_exception=False, set_status_on_exception=False
)
def _revise_findings(
    *,
    draft: InvestigationResult,
    review: PolicyReview,
    policies: list[PolicySource],
    model: BaseChatModel,
) -> InvestigationFindings:
    require_time()
    prompt = (Path(__file__).parent / "prompts" / "report_revision.md").read_text()
    response = policy_review_model(model).invoke(
        [
            (
                "system",
                prompt
                + "\n\nOutput schema:\n"
                + json.dumps(InvestigationFindings.model_json_schema()),
            ),
            (
                "human",
                json.dumps(
                    {
                        "policies": [policy.model_dump() for policy in policies],
                        "draft_findings": draft.findings.model_dump(mode="json"),
                        "policy_issues": review.model_dump(mode="json")["issues"],
                    }
                ),
            ),
        ],
        config={"run_name": "policy-faithfulness-revision"},
    )
    try:
        return InvestigationFindings.model_validate_json(response.text)
    except ValidationError as error:
        raise ReportValidationError(
            "Policy revision returned invalid output"
        ) from error


def _validation_receipt(
    *,
    policies: list[PolicySource],
    evaluation_count: int,
    revision_count: int,
) -> ReportValidation:
    return ReportValidation(
        policy_ids=[policy.id for policy in policies],
        evaluation_count=evaluation_count,
        revision_count=revision_count,
    )
