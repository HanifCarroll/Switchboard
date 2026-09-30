"""Definitions for live-model investigation evaluation cases."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, TypeAdapter

from switchboard.investigation.report_validation import PolicyIssueKind
from switchboard.models import Record, Text

EVALUATIONS = Path(__file__).resolve().parents[2] / "data" / "evaluations"


class InvestigationEvaluationCase(Record):
    scenario_id: Text
    expected_outcome: Literal["proposal_candidate", "blocked"]
    criteria: list[Text] = Field(min_length=1)


class PolicyFaithfulnessEvaluationCase(Record):
    id: Text
    investigation_output: Text | dict[str, Any]
    expect_issue: bool
    expected_issue_kinds: list[PolicyIssueKind] = Field(default_factory=list)
    expected_policy_ids: list[Text] = Field(default_factory=list)


def load_investigation_evaluation_cases() -> dict[str, InvestigationEvaluationCase]:
    """Load reference expectations that are never supplied to the agent."""
    content = (EVALUATIONS / "investigations.json").read_text()
    return TypeAdapter(dict[str, InvestigationEvaluationCase]).validate_json(content)


def load_policy_faithfulness_evaluation_cases() -> list[
    PolicyFaithfulnessEvaluationCase
]:
    """Load policy-judge calibration examples and their withheld expectations."""
    content = (EVALUATIONS / "policy_faithfulness.json").read_text()
    return TypeAdapter(list[PolicyFaithfulnessEvaluationCase]).validate_json(content)
