"""Accept signed webhook deliveries and retain one receipt per test event."""

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from botocore.exceptions import ClientError
from pydantic import HttpUrl, ValidationError

from switchboard.dynamodb import DynamoStore
from switchboard.models import Record, Text


class WebhookEvent(Record):
    workspace_id: Text
    generation: UUID
    test_event_id: UUID
    destination: HttpUrl


class WebhookReceipt(Record):
    test_event_id: UUID
    destination: HttpUrl
    payload_sha256: Text
    received_at: Text


def accept_event(event: WebhookEvent, *, payload_sha256: str, store: DynamoStore):
    """Keep a receiver receipt separate from the application's verification."""

    # 1. Build a receipt in the originating workspace generation.
    partition = f"WS#{event.workspace_id}#GEN#{event.generation}"
    sort = f"RECEIVER#{event.test_event_id}"
    receipt = WebhookReceipt(
        test_event_id=event.test_event_id,
        destination=event.destination,
        payload_sha256=payload_sha256,
        received_at=datetime.now(timezone.utc).isoformat(),
    )

    # 2. Commit once; an identical retransmission returns the first receipt.
    try:
        store.client.put_item(
            **store.put(partition, sort, receipt.model_dump(mode="json"), absent=True)[
                "Put"
            ]
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        previous = WebhookReceipt.model_validate_json(
            json.dumps(store.get(partition, sort))
        )
        if previous.payload_sha256 != payload_sha256:
            raise ValueError("The event ID has different inputs") from None

        return previous

    return receipt


def handler(event, context):
    """Function URL authentication is enforced by AWS IAM before this handler."""

    # 1. Require an authenticated HTTP delivery and a small JSON body.
    request = event.get("requestContext", {})
    if request.get("http", {}).get("method") != "POST":
        return _response(405, {"detail": "POST required"})

    if not request.get("authorizer", {}).get("iam"):
        return _response(403, {"detail": "Signed delivery required"})

    raw = event.get("body", "")
    if len(raw) > 8192:
        return _response(413, {"detail": "Event too large"})

    try:
        body = (
            base64.b64decode(raw, validate=True)
            if event.get("isBase64Encoded")
            else raw.encode()
        )
        if len(body) > 4096:
            return _response(413, {"detail": "Event too large"})

        delivery = WebhookEvent.model_validate_json(body)
        UUID(delivery.workspace_id)
        destination_path = delivery.destination.path
        headers = {
            key.lower(): value for key, value in event.get("headers", {}).items()
        }
        if (
            delivery.destination.scheme != "https"
            or delivery.destination.host != headers.get("host")
            or delivery.destination.query
            or delivery.destination.fragment
            or not destination_path
            or destination_path != event.get("rawPath")
            or not destination_path.startswith("/destinations/")
        ):
            raise ValueError("Destination does not match the HTTP request")
    except (ValueError, ValidationError):
        return _response(400, {"detail": "Invalid webhook event"})

    # 2. Persist the observed receipt, rejecting reuse with different inputs.
    try:
        receipt = accept_event(
            delivery,
            payload_sha256=hashlib.sha256(body).hexdigest(),
            store=DynamoStore(),
        )
    except ValueError:
        return _response(409, {"detail": "Event ID conflict"})

    return _response(200, receipt.model_dump(mode="json"))


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
