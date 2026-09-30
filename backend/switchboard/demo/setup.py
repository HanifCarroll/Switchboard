"""Explicit demo setup and reset through the DynamoDB storage boundary."""

from typing import Literal

from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.models import DemoSetup
from switchboard.storage import WorkspaceStorage


def read_demo_setup(storage: WorkspaceStorage) -> DemoSetup | None:
    workspace = storage.get_workspace()
    if workspace is None:
        return None
    return DemoSetup(scenario_id=workspace["scenarioId"], inputs=workspace["inputs"])


def reset_demo(
    *,
    storage: WorkspaceStorage,
    scenario_id: str,
    identity_mode: Literal["demo", "entra", "cli", "eval"] = "demo",
) -> None:
    """Atomically replace one workspace with the requested scenario records."""
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise ValueError("Unknown scenario")
    payload = build_workspace_payload(
        scenario_id=scenario_id,
        selected_scenario=scenarios[scenario_id],
        identity_mode=identity_mode,
    )
    storage.reset_workspace(payload=payload)
