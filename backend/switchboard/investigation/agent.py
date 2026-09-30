"""Model configuration and LangGraph-backed agent construction."""

import json
from pathlib import Path

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ToolErrorMiddleware, before_model
from langchain_deepseek import ChatDeepSeek
from langgraph.runtime import Runtime

from switchboard.investigation.deadline import require_time
from switchboard.investigation.tools import TOOLS, InvestigationContext
from switchboard.models import InvestigationResult

MODEL = "deepseek-flash"


@before_model
def check_investigation_deadline(
    state: AgentState, runtime: Runtime[InvestigationContext | None]
):
    require_time()
    return None


def create_model():
    return ChatDeepSeek(
        model=MODEL,
        extra_body={"thinking": {"type": "enabled"}},
        max_tokens=8192,
        timeout=60,
        max_retries=1,
    )


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
