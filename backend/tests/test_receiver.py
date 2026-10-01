"""Exercise the HTTP sender and receiving handler against a durable workspace."""

import json
from uuid import uuid4

import httpx
import pytest

from switchboard.configuration import runtime_settings
from switchboard.demo.scenarios import build_workspace_payload, load_scenarios
from switchboard.integrations.delivery_service import send_synthetic_test_event
from switchboard.receiver import handler
from switchboard.storage import WorkspaceStorage
from tests.test_dynamodb import dynamo as dynamo

BASE = "https://receiver.lambda-url.us-east-1.on.aws"


@pytest.fixture
def receiver(dynamo, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DYNAMODB_TABLE", "switchboard")
    monkeypatch.setenv("SWITCHBOARD_RECEIVER_URL", BASE)
    storage = WorkspaceStorage(str(uuid4()), transport=dynamo)
    storage.reset_workspace(
        payload=build_workspace_payload(
            scenario_id="baseline",
            selected_scenario=load_scenarios()["baseline"],
            identity_mode="eval",
        )
    )
    observed = []

    def post(url, *, content, headers, timeout, follow_redirects):
        assert timeout == 10 and follow_redirects is False
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        request = httpx.Request("POST", url, content=content, headers=headers)
        event = {
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {"iam": {"userArn": "api-role"}},
            },
            "headers": {"host": request.url.host},
            "rawPath": request.url.path,
            "body": content.decode(),
        }
        result = handler(event, None)
        observed.append((event, result))
        return httpx.Response(
            result["statusCode"], content=result["body"], request=request
        )

    monkeypatch.setattr("switchboard.integrations.delivery_service.httpx.post", post)
    return storage, observed


def test_signed_delivery_receipt_duplicate_and_conflict(receiver):
    storage, observed = receiver
    integration = storage.get_integration(integration_id="int-acme-prod")
    assert integration
    destination = integration["endpoint"]
    assert destination.startswith(BASE + "/destinations/")
    event_id = str(uuid4())
    first = send_synthetic_test_event(
        destination=destination, test_event_id=event_id, storage=storage
    )
    second = send_synthetic_test_event(
        destination=destination, test_event_id=event_id, storage=storage
    )
    assert first.outcome == second.outcome == "delivered"
    assert observed[0][1] == observed[1][1]
    event = observed[0][0]
    body = json.loads(event["body"])
    body["destination"] += "/different"
    conflict = handler(
        {**event, "body": json.dumps(body), "rawPath": event["rawPath"] + "/different"},
        None,
    )
    assert conflict["statusCode"] == 409
    assert (
        handler({**event, "requestContext": {"http": {"method": "POST"}}}, None)[
            "statusCode"
        ]
        == 403
    )
    wrong_host = handler({**event, "headers": {"host": "unrelated.example"}}, None)
    assert wrong_host["statusCode"] == 400


def test_sender_does_not_accept_foreign_hosts_or_unmatched_receipts(
    receiver, monkeypatch
):
    storage, observed = receiver
    with pytest.raises(ValueError, match="controlled receiver"):
        send_synthetic_test_event(
            destination="https://external.example/destinations/x",
            test_event_id=str(uuid4()),
            storage=storage,
        )
    assert observed == []
    destination = BASE + "/destinations/customer.example/orders"
    monkeypatch.setattr(
        "switchboard.integrations.delivery_service.httpx.post",
        lambda *args, **kwargs: httpx.Response(200, json={"outcome": "delivered"}),
    )
    result = send_synthetic_test_event(
        destination=destination, test_event_id=str(uuid4()), storage=storage
    )
    assert result.outcome == "inconclusive"
    monkeypatch.setattr(
        "switchboard.integrations.delivery_service.httpx.post",
        lambda *args, **kwargs: httpx.Response(403),
    )
    assert (
        send_synthetic_test_event(
            destination=destination, test_event_id=str(uuid4()), storage=storage
        ).outcome
        == "failed"
    )


def test_timeout_after_acceptance_preserves_idempotent_receiver_receipt(
    receiver, monkeypatch
):
    storage, observed = receiver
    original = httpx.post
    destination = BASE + "/destinations/customer.example/orders"
    event_id = str(uuid4())

    def timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise httpx.ReadTimeout("Response lost")

    monkeypatch.setattr("switchboard.integrations.delivery_service.httpx.post", timeout)
    assert (
        send_synthetic_test_event(
            destination=destination, test_event_id=event_id, storage=storage
        ).outcome
        == "inconclusive"
    )
    monkeypatch.setattr(
        "switchboard.integrations.delivery_service.httpx.post", original
    )
    assert (
        send_synthetic_test_event(
            destination=destination, test_event_id=event_id, storage=storage
        ).outcome
        == "delivered"
    )
    assert observed[0][1] == observed[1][1]
    runtime_settings.cache_clear()
