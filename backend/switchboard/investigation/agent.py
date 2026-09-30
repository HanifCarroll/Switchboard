"""Model configuration and LangGraph-backed agent construction."""

import json
import os
from pathlib import Path

from botocore.config import Config
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ToolErrorMiddleware, before_model
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langgraph.runtime import Runtime

from switchboard.investigation.deadline import require_time
from switchboard.investigation.tools import TOOLS, InvestigationContext
from switchboard.models import InvestigationResult

MODEL = "deepseek-flash"
BEDROCK_MODEL_ID = "deepseek.v3.2"


@before_model
def check_investigation_deadline(
    state: AgentState, runtime: Runtime[InvestigationContext | None]
):
    require_time()
    return None


def create_model():
    provider = os.getenv("SWITCHBOARD_MODEL_PROVIDER", "deepseek")
    if provider == "bedrock":
        return ChatBedrockConverse(
            model=BEDROCK_MODEL_ID,
            region_name="us-east-1",
            credentials_profile_name=os.getenv("BEDROCK_PROFILE"),
            max_tokens=8192,
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
    if provider != "deepseek":
        raise ValueError("SWITCHBOARD_MODEL_PROVIDER must be bedrock or deepseek")

    return ChatDeepSeek(
        model=MODEL,
        extra_body={"thinking": {"type": "enabled"}},
        max_tokens=8192,
        timeout=60,
        max_retries=1,
    )


def policy_review_model(model: BaseChatModel):
    if isinstance(model, ChatDeepSeek):
        return model.bind(extra_body={"thinking": {"type": "disabled"}})
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

    # 2. Connect the model, read-only tools, error handling, and trusted context.
    return create_agent(
        model=model,
        tools=TOOLS,
        middleware=[
            check_investigation_deadline,
            ToolErrorMiddleware(on_error=explain_unavailable_record),
        ],
        context_schema=InvestigationContext,
        system_prompt=prompt.format(
            now=now,
            result_schema=json.dumps(InvestigationResult.model_json_schema()),
        ),
    )
