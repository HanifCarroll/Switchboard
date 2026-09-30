"""Exercise Bedrock tool calls and policy review without a paid model request."""

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from langchain_aws import ChatBedrockConverse

from switchboard.investigation.agent import BEDROCK_MODEL_ID, create_model
from switchboard.investigation.report_validation import evaluate_policy_claims
from switchboard.investigation.tools import TOOLS
from tests.test_report_validation import POLICIES, draft_report


def test_bedrock_tools_policy_review_and_access_failure():
    arguments = {
        "region_name": "us-east-1",
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }
    client = boto3.client("bedrock-runtime", **arguments)
    model = ChatBedrockConverse(
        model=BEDROCK_MODEL_ID,
        client=client,
        bedrock_client=boto3.client("bedrock", **arguments),
        max_tokens=8192,
    )
    expected = {
        "modelId": BEDROCK_MODEL_ID,
        "messages": ANY,
        "system": ANY,
        "inferenceConfig": {"maxTokens": 8192},
    }
    with Stubber(client) as stub:
        stub.add_response(
            "converse",
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "lookup-1",
                                    "name": "get_ticket",
                                    "input": {"ticket_id": "CHG-1042"},
                                }
                            }
                        ],
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30},
                "metrics": {"latencyMs": 10},
            },
            {**expected, "toolConfig": ANY},
        )
        response = model.bind_tools(TOOLS).invoke("Read CHG-1042.")
        assert response.tool_calls[0]["args"] == {"ticket_id": "CHG-1042"}

        stub.add_response(
            "converse",
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": '{"issues": []}'}],
                    }
                },
                "stopReason": "end_turn",
                "usage": {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30},
                "metrics": {"latencyMs": 10},
            },
            {**expected, "system": ANY},
        )
        review = evaluate_policy_claims(
            investigation_output=draft_report().model_dump(mode="json"),
            policies=POLICIES,
            model=model,
        )
        assert not review.issues

        stub.add_client_error("converse", service_error_code="AccessDeniedException")
        with pytest.raises(ClientError, match="AccessDeniedException"):
            model.invoke("Read CHG-1042.")
        stub.assert_no_pending_responses()


def test_unknown_provider_fails_before_model_creation(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MODEL_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="must be bedrock or deepseek"):
        create_model()
