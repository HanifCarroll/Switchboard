"""Check IAM signing, tool calls, policy review, and provider errors without inference."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from botocore.credentials import ReadOnlyCredentials
from langchain.agents.middleware import ModelRequest
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI
from openai import PermissionDeniedError
from pydantic import SecretStr

from switchboard.investigation import bedrock
from switchboard.investigation.agent import check_investigation_deadline, create_model
from switchboard.investigation.bedrock import HOST, BedrockSigV4Auth
from switchboard.investigation.deadline import investigation_deadline
from switchboard.investigation.report_validation import evaluate_policy_claims
from switchboard.investigation.tools import TOOLS, InvestigationContext
from switchboard.investigation.workflow import (
    EndpointChangeWorkflowState,
    investigate_request,
)
from tests.test_report_validation import POLICIES, draft_report


@pytest.mark.parametrize(
    "model_id", ["deepseek.v3.2", "openai.gpt-6-luna", "minimax.minimax-m2.5"]
)
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


@pytest.mark.parametrize("evidence_complete", [False, True])
def test_minimax_native_report_format_waits_for_evidence(evidence_complete):
    model = ChatOpenAI(model="minimax.minimax-m2.5", api_key=SecretStr("unused"))
    required_tools = ["get_ticket", "get_customer", "list_policies"]
    if evidence_complete:
        required_tools.append("get_integration")
    request: ModelRequest[InvestigationContext | None] = ModelRequest(
        model=model,
        tools=[*TOOLS],
        messages=[
            ToolMessage(
                content="retrieved or unavailable", name=name, tool_call_id=name
            )
            for name in required_tools
        ],
    )
    handler = Mock()
    check_investigation_deadline.wrap_model_call(request, handler)
    forwarded_request = handler.call_args.args[0]
    if evidence_complete:
        assert forwarded_request.tools == []
        assert isinstance(forwarded_request.response_format, ProviderStrategy)
    else:
        assert forwarded_request.tools == TOOLS
        assert forwarded_request.response_format is None


@pytest.mark.parametrize("deadline_expires", [False, True])
def test_minimax_stream_assembles_tool_arguments_and_stops_at_deadline(
    monkeypatch, deadline_expires
):

    # 1. Supply credentials and split a tool argument across streamed chunks.
    session = Mock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = (
        ReadOnlyCredentials("AKID", "secret", "token")
    )
    monkeypatch.setattr(bedrock.boto3, "Session", lambda **kwargs: session)
    deadline = time.monotonic() + 300

    class ToolStream(httpx.SyncByteStream):
        def __iter__(self):
            for index, arguments in enumerate(['{"ticket_', 'id":"CHG-1042"}']):
                if index == 1 and deadline_expires:
                    monkeypatch.setattr(time, "monotonic", lambda: deadline + 1)
                call = {
                    "index": 0,
                    "type": "function",
                    "function": {"arguments": arguments},
                }
                if index == 0:
                    call["id"] = "lookup-1"
                    call["function"]["name"] = "get_ticket"
                chunk = {
                    "id": "completion-1",
                    "object": "chat.completion.chunk",
                    "model": "minimax.minimax-m2.5",
                    "created": 0,
                    "choices": [{"index": 0, "delta": {"tool_calls": [call]}}],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

    def respond(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["parallel_tool_calls"] is False
        assert body["max_completion_tokens"] == 20_000
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ToolStream(),
        )

    # 2. Run the real model adapter against the controlled stream.
    original_client = httpx.Client

    class TestClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(bedrock.httpx, "Client", TestClient)
    model = bedrock.create_bedrock_model("minimax.minimax-m2.5")

    # 3. Preserve complete arguments and reject work past the job deadline.
    with investigation_deadline(deadline):
        if deadline_expires:
            with pytest.raises(TimeoutError, match="Investigation deadline reached"):
                model.bind_tools(TOOLS).invoke("Read CHG-1042.")
        else:
            response = model.bind_tools(TOOLS).invoke("Read CHG-1042.")
            assert response.tool_calls[0]["args"] == {"ticket_id": "CHG-1042"}
    assert model.http_client is not None
    model.http_client.close()
