"""Keep signed origin identity and scheduled capability outside application auth."""

from switchboard import lambda_handler


def test_origin_signing_preserves_application_token_and_persona_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lambda_handler, "http_handler", lambda event, context: calls.append(event) or {}
    )
    lambda_handler.handler(
        {
            "requestContext": {},
            "headers": {
                "Authorization": "AWS4-HMAC-SHA256 signature",
                "X-Switchboard-Authorization": "Bearer token",
                "X-Demo-Persona-Id": "emp-alex",
            },
        },
        None,
    )
    assert calls[0]["headers"]["authorization"] == "Bearer token"
    assert calls[0]["headers"]["x-demo-persona-id"] == "emp-alex"
    lambda_handler.handler(
        {
            "requestContext": {},
            "headers": {"Authorization": "AWS4-HMAC-SHA256 signature"},
        },
        None,
    )
    assert "authorization" not in calls[1]["headers"]


def test_http_body_cannot_invoke_maintenance(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lambda_handler, "http_handler", lambda event, context: calls.append(event) or {}
    )
    lambda_handler.handler(
        {
            "source": "switchboard.maintenance",
            "requestContext": {},
            "body": '{"source":"switchboard.maintenance"}',
        },
        None,
    )
    assert len(calls) == 1
