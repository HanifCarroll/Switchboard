"""Synthetic scenario setup for demo workspaces and live-model evals."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, TypeAdapter

from switchboard.models import Customer, Employee, Integration, Record, Text, Ticket
from switchboard.storage import WorkspaceStorage

DATA = Path(__file__).resolve().parents[2] / "data"
FIXTURES = DATA / "fixtures"
SCENARIOS = DATA / "scenarios"


class TicketUpdates(Record):
    requested_endpoint: HttpUrl | None = None
    body: Text | None = None
    requester_contact_id: Text | None = None


class Scenario(Record):
    now: AwareDatetime | None = None
    request: Text | None = None
    ticket_updates: TicketUpdates = Field(default_factory=TicketUpdates)


def load_scenarios() -> dict[str, Scenario]:
    content = (SCENARIOS / "investigations.json").read_text()
    return TypeAdapter(dict[str, Scenario]).validate_json(content)


def build_workspace_payload(
    *,
    scenario_id: str,
    selected_scenario: Scenario,
    identity_mode: Literal["demo", "entra", "cli", "eval"],
    updated_at: datetime | None = None,
) -> dict:
    """Build one validated DynamoDB workspace reset without writing partial records."""
    # 1. Validate every fixture before preparing any storage operation.
    records = {}
    for table, model in (
        ("customers", Customer),
        ("employees", Employee),
        ("integrations", Integration),
        ("tickets", Ticket),
    ):
        content = (FIXTURES / f"{table}.json").read_text()
        TypeAdapter(list[model]).validate_json(content)
        records[table] = json.loads(content)

    records["policies"] = [
        {"id": path.stem, "content": path.read_text()}
        for path in sorted((FIXTURES / "policies").glob("*.md"))
    ]

    # 2. Apply the selected scenario to the request inputs and ticket copy.
    inputs = json.loads((SCENARIOS / "base-inputs.json").read_text())
    if selected_scenario.now is not None:
        inputs["now"] = selected_scenario.now.isoformat()
    if selected_scenario.request is not None:
        inputs["request"] = selected_scenario.request

    updates = selected_scenario.ticket_updates.model_dump(
        mode="json", exclude_none=True
    )
    if updates:
        ticket = next(
            (item for item in records["tickets"] if item["id"] == inputs["ticket_id"]),
            None,
        )
        if ticket is None:
            raise ValueError("Scenario ticket is missing")
        ticket.update(updates)
        Ticket.model_validate_json(json.dumps(ticket))

    return {
        "identityMode": identity_mode,
        "scenarioId": scenario_id,
        "inputs": inputs,
        "updatedAt": (updated_at or datetime.now(timezone.utc)).isoformat(),
        "records": records,
    }


def initialize_demo_workspace(
    *,
    storage: WorkspaceStorage,
    scenario_id: str,
    selected_scenario: Scenario,
    identity_mode: Literal["demo", "entra", "cli", "eval"] = "demo",
) -> dict:
    """Initialize once and refuse implicit changes to an existing scenario."""
    existing = storage.get_workspace()
    if existing is None:
        payload = build_workspace_payload(
            scenario_id=scenario_id,
            selected_scenario=selected_scenario,
            identity_mode=identity_mode,
        )
        storage.reset_workspace(payload=payload)
        return payload["inputs"]

    if existing["scenarioId"] != scenario_id:
        raise ValueError("Reset the demo before selecting a different scenario")
    return existing["inputs"]
