"""Keep live-model evaluation references aligned with scenario setup."""

from switchboard.demo.scenarios import load_scenarios
from switchboard.investigation.evaluation_cases import (
    load_investigation_evaluation_cases,
    load_policy_faithfulness_evaluation_cases,
)
from switchboard.models import InvestigationResult


def test_every_investigation_scenario_has_one_evaluation_case():
    scenarios = load_scenarios()
    evaluation_cases = load_investigation_evaluation_cases()

    assert set(evaluation_cases) == set(scenarios)
    assert all(
        case.scenario_id == case_id for case_id, case in evaluation_cases.items()
    )


def test_policy_cases_accept_complete_structured_reports():
    cases = load_policy_faithfulness_evaluation_cases()
    reports = [case for case in cases if isinstance(case.investigation_output, dict)]
    assert reports
    for case in reports:
        InvestigationResult.model_validate(case.investigation_output)
