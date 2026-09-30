"""Check IAM signing, tool calls, policy review, and provider errors without inference."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from botocore.credentials import ReadOnlyCredentials
from langchain_openai import ChatOpenAI
from openai import PermissionDeniedError
from pydantic import SecretStr

from switchboard.investigation.agent import create_model
from switchboard.investigation.bedrock import HOST, BedrockSigV4Auth
from switchboard.investigation.report_validation import evaluate_policy_claims
from switchboard.investigation.tools import TOOLS
from switchboard.investigation.workflow import (
    EndpointChangeWorkflowState,
    investigate_request,
)
from tests.test_report_validation import POLICIES, draft_report


@pytest.mark.parametrize("model_id", ["deepseek.v3.2", "openai.gpt-6-luna"])
def test_bedrock_signing_tools_policy_review_and_access_failure(model_id):
    session = Mock()
    credentials = session.get_credentials.return_value
    credentials.get_frozen_credentials.side_effect = [
        ReadOnlyCredentials("AKIDONE", "secret-one", "token-one"),
        ReadOnlyCredentials("AKIDTWO", "secret-two", "token-two"),
        ReadOnlyCredentials("AKIDTHREE", "secret-three", "token-three"),
    ]
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append((dict(request.headers), body))
        assert body["model"] == model_id
        assert "thinking" not in body
        if len(calls) == 3:
            return httpx.Response(403, json={"error": {"message": "Denied"}})
        message = (
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "lookup-1",
                        "type": "function",
                        "function": {
                            "name": "get_ticket",
                            "arguments": '{"ticket_id":"CHG-1042"}',
                        },
                    }
                ],
            }
            if "tools" in body
            else {"content": '{"issues": []}'}
        )
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "model": model_id,
                "object": "chat.completion",
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if "tools" in body else "stop",
                        "message": {"role": "assistant", **message},
                    }
                ],
            },
        )

    auth = BedrockSigV4Auth(session, model_id)
    with httpx.Client(auth=auth, transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(
            model=model_id,
            base_url=f"https://{HOST}{auth.base_path}",
            api_key=SecretStr("aws-sigv4"),
            http_client=client,
            max_retries=0,
        )
        response = model.bind_tools(TOOLS).invoke("Read CHG-1042.")
        assert response.tool_calls[0]["args"] == {"ticket_id": "CHG-1042"}
        review = evaluate_policy_claims(
            investigation_output=draft_report().model_dump(mode="json"),
            policies=POLICIES,
            model=model,
        )
        assert not review.issues
        with pytest.raises(PermissionDeniedError):
            model.invoke("Read CHG-1042.")

    for index, key in enumerate(["AKIDONE", "AKIDTWO", "AKIDTHREE"]):
        assert f"Credential={key}/" in calls[index][0]["authorization"]
        assert (
            "/us-east-1/bedrock-mantle/aws4_request" in calls[index][0]["authorization"]
        )
        assert calls[index][0]["x-amz-security-token"].startswith("token-")
    assert "tools" not in calls[1][1]
    assert calls[1][1]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("returned_ticket", ["CHG-1042", "CHG-1043"])
def test_structured_result_preserves_the_selected_ticket(returned_ticket):
    draft = draft_report().model_copy(update={"ticket_id": returned_ticket})
    context = SimpleNamespace(
        agent=Mock(),
        investigation_context=Mock(),
        captured_at=datetime.now(timezone.utc),
    )
    context.agent.invoke.return_value = {"structured_response": draft, "messages": []}
    runtime = Mock(context=context)
    state: EndpointChangeWorkflowState = {
        "request": "Investigate CHG-1042",
        "ticket_id": "CHG-1042",
    }

    if returned_ticket != state["ticket_id"]:
        with pytest.raises(ValueError, match="different ticket"):
            investigate_request(state, runtime)
    else:
        result = investigate_request(state, runtime)
        assert result["investigation"] == draft
        assert result["evidence"] == []


def test_bedrock_never_signs_another_destination():
    session = Mock()
    with httpx.Client(auth=BedrockSigV4Auth(session)) as client:
        with pytest.raises(ValueError, match="Unsupported Bedrock endpoint"):
            client.post("https://example.com/v1/chat/completions", json={})
    session.get_credentials.assert_not_called()


def test_unknown_provider_fails_before_model_creation(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MODEL_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="must be bedrock or deepseek"):
        create_model()
