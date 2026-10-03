"""Model configuration and LangGraph-backed agent construction."""

import json
import os
from collections.abc import Callable
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    ToolErrorMiddleware,
    wrap_model_call,
)
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from opentelemetry import trace
from pydantic import SecretStr

from switchboard.configuration import deepseek_api_key
from switchboard.investigation.bedrock import create_bedrock_model
from switchboard.investigation.deadline import require_time
from switchboard.investigation.tools import TOOLS, InvestigationContext
from switchboard.models import InvestigationResult

MODEL = "deepseek-flash"


@wrap_model_call
def check_investigation_deadline(
    request: ModelRequest[InvestigationContext | None],
    handler: Callable[[ModelRequest[InvestigationContext | None]], ModelResponse],
) -> ModelResponse:

    # 1. Reserve time for a bounded model request and retry.
    require_time()

    # 2. Finish MiniMax evidence collection before requiring a native JSON report.
    completed_tools = {
        message.name for message in request.messages if isinstance(message, ToolMessage)
    }
    if (
        isinstance(request.model, ChatOpenAI)
        and request.model.model_name == "minimax.minimax-m2.5"
        and {"get_ticket", "get_customer", "get_integration", "list_policies"}
        <= completed_tools
    ):
        request = request.override(
            tools=[], response_format=ProviderStrategy(InvestigationResult, strict=True)
        )

    # 3. Trace the model call without recording customer content.
    with trace.get_tracer(__name__).start_as_current_span(
        "Investigation model", record_exception=False, set_status_on_exception=False
    ):
        return handler(request)


def create_model():
    provider = os.getenv("SWITCHBOARD_MODEL_PROVIDER", "deepseek")
    if provider == "bedrock":
        return create_bedrock_model()
    if provider != "deepseek":
        raise ValueError("SWITCHBOARD_MODEL_PROVIDER must be bedrock or deepseek")

    key = deepseek_api_key()
    return ChatDeepSeek(
        model=MODEL,
        api_key=SecretStr(key) if key else None,
        extra_body={"thinking": {"type": "enabled"}},
        max_tokens=8192,
        timeout=60,
        max_retries=1,
    )


def policy_review_model(model: BaseChatModel):
    if isinstance(model, ChatDeepSeek):
        return model.bind(
            extra_body={"thinking": {"type": "enabled"}},
            reasoning_effort="low",
            max_tokens=16384,
        )
    if isinstance(model, ChatOpenAI):
        if model.model_name == "minimax.minimax-m2.5":
            return model.bind(
                response_format={"type": "json_object"},
                max_completion_tokens=25_000,
            )
        return model.bind(response_format={"type": "json_object"})
    return model


def explain_unavailable_record(error: Exception, request) -> str | None:
    """Handle expected access failures without disclosing record existence."""
    # 1. Turn access failures into a safe message without revealing existence.
    if isinstance(error, PermissionError):
        return (
            "I couldn't retrieve that record. It may not exist, "
            "or you may not have permission to access it."
        )

    # 2. Let unexpected errors fail the run.
    return None  # Unexpected failures must still fail the run.


def build_agent(*, model, now: str):
    """Investigate with thinking and request a final JSON result."""
    # 1. Load the investigation instructions.
    prompt = (Path(__file__).parent / "prompts" / "investigation.md").read_text()
    response_format = None
    if isinstance(model, ChatOpenAI) and model.model_name != "minimax.minimax-m2.5":
        response_format = ToolStrategy(InvestigationResult)
        prompt += (
            "\n\nWhen evidence collection is complete, call the InvestigationResult "
            "tool with the final report. Do not return the report as prose or call "
            "the read-only tools again after retrieving the required records."
        )

    # 2. Connect the model, read-only tools, error handling, and trusted context.
    return create_agent(
        model=model,
        tools=TOOLS,
        middleware=[
            check_investigation_deadline,
            ToolErrorMiddleware(on_error=explain_unavailable_record),
        ],
        context_schema=InvestigationContext,
        response_format=response_format,
        system_prompt=prompt.format(
            now=now,
            result_schema=json.dumps(InvestigationResult.model_json_schema()),
        ),
    )
