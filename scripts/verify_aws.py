#!/usr/bin/env python3
"""Real DynamoDB receipts, chunking, and reset fences in an isolated test workspace."""
# ruff: noqa: E402 -- Load the repository's backend before importing its modules.

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from switchboard.change_management import (
    approve_proposal,
    execute_proposal,
)
from switchboard.delivery_verification import verify_execution_delivery
from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.dynamodb import DynamoStore
from switchboard.integrations.employee_directory import EmployeeSession
from switchboard.storage import StorageError, WorkspaceStorage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--table", default="switchboard")
    parser.add_argument("--endpoint-url")
    arguments = parser.parse_args()
    session = boto3.Session(profile_name=arguments.profile, region_name="us-east-1")
    boto3.setup_default_session(profile_name=arguments.profile, region_name="us-east-1")
    if not arguments.endpoint_url:
        os.environ["SWITCHBOARD_CONFIG_PARAMETER"] = "/switchboard/live/runtime"
    store = DynamoStore(
        arguments.table, session.client("dynamodb", endpoint_url=arguments.endpoint_url)
    )
    storage = WorkspaceStorage(str(uuid4()), transport=store)
    checks = []

    # 1. Exercise the actual AWS transactions using the same business operations.
    initialize_demo_portfolio(storage=storage)
    proposal = next(
        item
        for item in storage.list_pending_proposals()
        if item["ticket_id"] == "CHG-1045"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda identifier: storage.save_proposal(
                    proposal={**proposal, "id": identifier}
                ),
                [str(uuid4()), str(uuid4())],
            )
        )
    assert all(result["proposal"]["id"] == proposal["id"] for result in results)
    checks.append("canonical duplicate proposals")
    author = EmployeeSession(storage=storage, employee_id="emp-alex")
    reviewer = EmployeeSession(storage=storage, employee_id="emp-priya")
    approve_proposal(proposal_id=proposal["id"], session=reviewer)
    first = execute_proposal(
        proposal_id=proposal["id"],
        session=author,
        executed_at=datetime.now(timezone.utc),
    )
    second = execute_proposal(
        proposal_id=proposal["id"],
        session=author,
        executed_at=datetime.now(timezone.utc),
    )
    assert (
        first.execution == second.execution
        and first.was_created
        and not second.was_created
    )
    checks.append("one execution receipt/version increment")
    if not arguments.endpoint_url:
        verification = verify_execution_delivery(
            proposal_id=proposal["id"],
            session=author,
            verified_at=datetime.now(timezone.utc),
        )
        ticket = storage.get_ticket(ticket_id=proposal["ticket_id"])
        assert verification.was_created and ticket and ticket["status"] == "closed"
        checks.append("signed receiver delivery and atomic verification/ticket outcome")
    integration = storage.get_integration(integration_id=proposal["integration_id"])
    assert (
        integration
        and integration["version"] == proposal["expected_configuration_version"] + 1
    )

    # 2. Confirm chunked UTF-8 results survive a fresh reader and paginated history.
    run = {
        "id": str(uuid4()),
        "ticket_id": "CHG-1042",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": {"text": "á🙂" * 70000, "proposal": None},
    }
    storage.save_run(run=run)
    fresh = WorkspaceStorage(
        storage.workspace_id, transport=DynamoStore(store.table, store.client)
    )
    assert fresh.get_run(run_id=run["id"]) == run
    assert fresh.list_runs(ticket_id="CHG-1042") == [run]
    checks.append("multi-item Unicode result roundtrip")

    # 3. A reset prevents writes through a request holding the retired generation.
    initialize_demo_portfolio(storage=storage)
    try:
        fresh.save_proposal(proposal={"id": "retired", "created_at": "now"})
    except StorageError as error:
        assert error.status == 409
    else:
        raise AssertionError("Retired generation accepted a write")
    checks.append("reset fences old requests")
    storage.delete_workspace()
    receipt = {
        "workspace_id": storage.workspace_id,
        "checks": checks,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    path = ROOT / "aws" / "local" / "dynamodb-verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
