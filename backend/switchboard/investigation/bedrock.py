"""IAM-authenticated Bedrock chat completions with metadata-only tracing."""

import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import SecretStr

from switchboard.investigation.deadline import require_time

MODEL_ID = "deepseek.v3.2"
HOST = "bedrock-mantle.us-east-1.api.aws"
tracer = trace.get_tracer(__name__)


class StreamingDeadline(BaseCallbackHandler):
    """Stop streamed responses when the queued investigation runs out of time."""

    raise_error = True

    def on_llm_new_token(self, token, **kwargs):
        require_time(seconds=0)


class BedrockSigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, session, model_id=MODEL_ID):
        if model_id not in {
            "deepseek.v3.2",
            "openai.gpt-6-luna",
            "minimax.minimax-m2.5",
        }:
            raise ValueError("Unsupported Bedrock model")
        self.session = session
        self.model_id = model_id
        self.base_path = "/openai/v1" if model_id.startswith("openai.") else "/v1"

    def auth_flow(self, request):

        # 1. Restrict credential signing to the configured Bedrock endpoint.
        if (
            request.url.scheme != "https"
            or request.url.host != HOST
            or request.url.path != f"{self.base_path}/chat/completions"
        ):
            raise ValueError("Unsupported Bedrock endpoint")
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials are required for Bedrock")

        # 2. Refresh credentials and sign the exact request body on every attempt.
        signed = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(
            credentials.get_frozen_credentials(), "bedrock-mantle", "us-east-1"
        ).add_auth(signed)
        request.headers.update(dict(signed.headers))

        # 3. Trace timing and status without recording prompts, records, or headers.
        with tracer.start_as_current_span(
            "Bedrock chat completion",
            kind=SpanKind.CLIENT,
            attributes={
                "rpc.system": "aws-api",
                "rpc.service": "bedrock-mantle",
                "rpc.method": "CreateInference",
                "server.address": HOST,
                "gen_ai.request.model": self.model_id,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            response = yield request
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR))


def create_bedrock_model(model_id=None):
    model_id = model_id or os.getenv("SWITCHBOARD_BEDROCK_MODEL", MODEL_ID)
    session = boto3.Session(
        profile_name=os.getenv("BEDROCK_PROFILE"), region_name="us-east-1"
    )
    auth = BedrockSigV4Auth(session, model_id)
    use_streaming = model_id == "minimax.minimax-m2.5"
    return ChatOpenAI(
        model=model_id,
        base_url=f"https://{HOST}{auth.base_path}",
        # HTTP authentication replaces this SDK placeholder with an IAM signature.
        api_key=SecretStr("aws-sigv4"),
        http_client=httpx.Client(auth=auth),
        http_async_client=httpx.AsyncClient(auth=auth),
        reasoning_effort="none" if model_id.startswith("openai.") else None,
        # Use streamed, sequential tool calls for MiniMax.
        streaming=use_streaming,
        stream_usage=True,
        model_kwargs={"parallel_tool_calls": False} if use_streaming else {},
        callbacks=[StreamingDeadline()] if use_streaming else None,
        temperature=0,
        max_completion_tokens=8192,
        service_tier="default",
        timeout=60,
        max_retries=1,
    )
