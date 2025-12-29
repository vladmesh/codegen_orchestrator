"""Intent Parser node for Dynamic ProductOwner.

Classifies user intent and selects capabilities for ProductOwner.
Only runs for NEW tasks (no active checkpoint).

Flow: START → intent_parser → product_owner
"""

from langchain_core.messages import HumanMessage, SystemMessage
import structlog

from ..capabilities import list_available_capabilities
from ..clients.api import api_client
from ..llm.factory import LLMFactory
from ..thread_manager import generate_thread_id
from .base import log_node_execution

logger = structlog.get_logger()

# Intent parser uses cheap, fast model
PARSER_MODEL = "openai/gpt-4o-mini"
PARSER_TEMPERATURE = 0.0

# System prompt for intent classification
INTENT_PARSER_PROMPT = """You are an intent classifier for a DevOps automation system.

Your job is to analyze the user's message and select which CAPABILITIES
the ProductOwner agent will need.

Available capabilities:
{capabilities}

Rules:
1. Select ONLY the capabilities needed for this specific request
2. If unclear, prefer fewer capabilities - PO can request more later
3. Simple questions (list projects, status) → project_management only
4. Deploy requests → deploy + infrastructure
5. Debug/error questions → diagnose
6. New project creation → project_management + engineering

Respond ONLY with valid JSON:
{{
    "capabilities": ["capability1", "capability2"],
    "task_summary": "Brief description of what user wants",
    "reasoning": "Why these capabilities were chosen"
}}
"""


def _format_capabilities_for_prompt() -> str:
    """Format capability descriptions for the prompt."""
    caps = list_available_capabilities()
    lines = []
    for name, desc in caps.items():
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


async def _get_recent_messages(user_id: int, limit: int = 3) -> list[str]:
    """Get recent conversation summaries for context."""
    try:
        summaries = await api_client.get(f"rag/summaries?user_id={user_id}&limit={limit}")
        if summaries:
            return [s.get("summary_text", "") for s in summaries if s.get("summary_text")]
    except Exception as e:
        logger.warning("intent_parser_context_fetch_failed", error=str(e))
    return []


def _parse_llm_response(content: str) -> dict:
    """Parse JSON response from LLM, handling markdown code blocks."""
    import json

    # Strip markdown code blocks if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("intent_parser_json_parse_failed", error=str(e), content=content[:200])
        # Fallback: return minimal capabilities
        return {
            "capabilities": ["project_management"],
            "task_summary": "User request",
            "reasoning": "Parse failed, using safe default",
        }


@log_node_execution("intent_parser")
async def run(state: dict) -> dict:
    """Run Intent Parser to classify user message and select capabilities.

    Args:
        state: Graph state with messages and user info

    Returns:
        State update with:
        - thread_id: new thread ID for this task
        - active_capabilities: selected capabilities
        - task_summary: brief task description
    """
    messages = state.get("messages", [])
    telegram_user_id = state.get("telegram_user_id")

    # Get the latest user message
    user_message = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content
            break

    if not user_message:
        logger.warning("intent_parser_no_user_message")
        return {
            "active_capabilities": ["project_management"],
            "task_summary": "Unknown request",
        }

    # Get context from recent conversations
    context_messages = []
    if telegram_user_id:
        context_messages = await _get_recent_messages(telegram_user_id)

    # Build context string
    context_str = ""
    if context_messages:
        context_str = f"\n\nRecent conversation context:\n{chr(10).join(context_messages)}"

    # Format prompt
    capabilities_text = _format_capabilities_for_prompt()
    system_prompt = INTENT_PARSER_PROMPT.format(capabilities=capabilities_text)

    # Create LLM (cheap, fast model)
    llm = LLMFactory.create_llm(
        {
            "model_name": PARSER_MODEL,
            "temperature": PARSER_TEMPERATURE,
        }
    )

    # Build messages for LLM
    llm_messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"User message: {user_message}{context_str}"),
    ]

    # Get classification
    response = await llm.ainvoke(llm_messages)
    parsed = _parse_llm_response(response.content)

    capabilities = parsed.get("capabilities", ["project_management"])
    task_summary = parsed.get("task_summary", "User request")
    reasoning = parsed.get("reasoning", "")

    # Generate new thread_id for this task
    new_thread_id = None
    if telegram_user_id:
        new_thread_id = await generate_thread_id(telegram_user_id)

    logger.info(
        "intent_parsed",
        capabilities=capabilities,
        task_summary=task_summary,
        reasoning=reasoning,
        thread_id=new_thread_id,
    )

    return {
        "thread_id": new_thread_id,
        "active_capabilities": capabilities,
        "task_summary": task_summary,
        "current_agent": "intent_parser",
    }
