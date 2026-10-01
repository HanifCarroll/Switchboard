"""HTTP adaptation and an IAM-invoked maintenance event."""

import logging

from mangum import Mangum

from switchboard.api import app

logging.getLogger().setLevel(logging.INFO)
http_handler = Mangum(app)


def handler(event, context):
    if event == {"source": "switchboard.maintenance"}:
        from switchboard.maintenance import maintain

        return maintain(context)
    if event.get("source") == "switchboard.workflow" and "requestContext" not in event:
        from switchboard.workflow import handle

        return handle(event)
    if "requestContext" not in event:
        raise ValueError("Unsupported invocation")

    # CloudFront signing identifies infrastructure; the dedicated header identifies users.
    headers = {name.lower(): value for name, value in event.get("headers", {}).items()}
    authorization = headers.get("authorization", "")
    if authorization.startswith("AWS4-HMAC-SHA256 "):
        headers.pop("authorization")
    application_token = headers.pop("x-switchboard-authorization", None)
    if application_token is not None:
        if "authorization" in headers:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": '{"detail":"Ambiguous application identity"}',
            }
        headers["authorization"] = application_token
    return http_handler({**event, "headers": headers}, context)
