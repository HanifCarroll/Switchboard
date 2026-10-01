"""Send a signed HTTP event to the controlled receiver and validate its receipt."""

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from switchboard.configuration import runtime_settings
from switchboard.dynamodb import DynamoStore
from switchboard.models import DeliveryOutcome
from switchboard.receiver import WebhookEvent, WebhookReceipt
from switchboard.storage import WorkspaceStorage


@dataclass(frozen=True)
class DeliveryTestResult:
    outcome: DeliveryOutcome
    test_event_id: str
    evidence: str


def send_synthetic_test_event(
    *, destination: str, test_event_id: str, storage: WorkspaceStorage
) -> DeliveryTestResult:
    """A timeout remains uncertain; a matching receiver receipt proves delivery."""

    # 1. Restrict outbound requests to the configured receiver, without redirects.
    base = runtime_settings().receiver_url
    target = urlsplit(destination)
    if (
        base is None
        or target.scheme != "https"
        or not target.hostname
        or target.hostname != base.host
        or not target.hostname.endswith(".lambda-url.us-east-1.on.aws")
        or target.port not in {None, 443}
        or target.username is not None
        or target.query
        or target.fragment
        or not target.path.startswith("/destinations/")
    ):
        raise ValueError("Destination is not a registered controlled receiver")

    if not isinstance(storage.transport, DynamoStore):
        raise ValueError("Receiver delivery requires a durable workspace")

    event = WebhookEvent.model_validate_json(
        json.dumps(
            {
                "workspace_id": storage.workspace_id,
                "generation": storage.transport.generation(storage.workspace_id),
                "test_event_id": test_event_id,
                "destination": destination,
            }
        )
    )
    body = event.model_dump_json().encode()
    digest = hashlib.sha256(body).hexdigest()

    # 2. Sign with the API execution role and send one bounded HTTP request.
    session = boto3.DEFAULT_SESSION or boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are unavailable for delivery")

    request = AWSRequest(
        method="POST",
        url=destination,
        data=body,
        headers={"content-type": "application/json", "x-amz-content-sha256": digest},
    )
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", "us-east-1").add_auth(
        request
    )
    try:
        response = httpx.post(
            destination,
            content=body,
            headers=dict(request.headers),
            timeout=10,
            follow_redirects=False,
        )
    except httpx.RequestError:
        return DeliveryTestResult(
            outcome="inconclusive",
            test_event_id=test_event_id,
            evidence="No receiver response was obtained. Receipt of the event is unknown; manual investigation is required.",
        )

    # 3. Confirm the receiver observed this exact event at the approved destination.
    if response.status_code != 200:
        return DeliveryTestResult(
            outcome="failed" if 400 <= response.status_code < 500 else "inconclusive",
            test_event_id=test_event_id,
            evidence=f"Receiver returned HTTP {response.status_code}; successful delivery was not confirmed.",
        )
    try:
        receipt = WebhookReceipt.model_validate_json(response.content)
        if (
            receipt.test_event_id != event.test_event_id
            or receipt.destination != event.destination
            or receipt.payload_sha256 != digest
        ):
            raise ValueError("Receipt does not match the event")
    except ValueError:
        return DeliveryTestResult(
            outcome="inconclusive",
            test_event_id=test_event_id,
            evidence="Receiver response did not establish receipt of the expected event.",
        )
    return DeliveryTestResult(
        outcome="delivered",
        test_event_id=test_event_id,
        evidence=f"Receiver accepted event {test_event_id} at {receipt.received_at}; HTTP 200 and payload SHA-256 {digest} match the sent event.",
    )
