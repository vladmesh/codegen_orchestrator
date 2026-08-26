"""PO ReactAgent tools.

Async tools for the Product Owner agent. Uses the shared internal API client and
the Redis client, both initialized at consumer startup via init_po_clients().

This module re-exports all tools from sub-modules for backward compatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import re
import time

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.engineering_budget_display import format_microusd
from shared.queues import PO_PROACTIVE_QUEUE, PO_REMINDERS_KEY

# Re-export project tools
from .tools_projects import (  # noqa: F401
    AVAILABLE_MODULES,
    HTTP_UNPROCESSABLE,
    add_bot_user,
    create_project,
    get_project,
    list_projects,
    remove_bot_user,
    set_bot_access,
    set_project_secret,
    teardown_project,
    validate_telegram_token,
)

# Re-export shared helpers so existing patch targets still work:
#   patch("src.agents.po.tools._get_api", ...)
#   patch("src.agents.po.tools.init_po_clients", ...)
from .tools_shared import (  # noqa: F401
    _get_api,
    _get_stream_client,
    _user_headers,
    init_po_clients,
)

# Re-export story/run tools
from .tools_stories import (  # noqa: F401
    create_story,
    get_run_status,
    get_story,
    list_stories,
    reopen_story,
)

logger = structlog.get_logger(__name__)

_STORY_ID_RE = re.compile(r"\bstory-[A-Za-z0-9-]+\b")


# ---------------------------------------------------------------------------
# Tools that live in this module (utility / non-domain)
# ---------------------------------------------------------------------------


@tool
async def set_reminder(delay_minutes: int, reason: str, *, config: RunnableConfig) -> str:
    """Set a reminder to wake up after a delay.

    Use this whenever you need to wait and follow up later — after triggering
    a task, when the user asks to be reminded, or any situation where you
    should check back in the future.

    Args:
        delay_minutes: Minutes until reminder fires.
        reason: Why you're setting this reminder (e.g. "check engineering task eng-abc123").
    """
    redis = _get_stream_client().redis
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    fire_at = time.time() + delay_minutes * 60
    story_match = _STORY_ID_RE.search(reason)

    reminder = json.dumps(
        {
            "type": "reminder",
            "telegram_chat_id": telegram_chat_id,
            "text": reason,
            "story_id": story_match.group(0) if story_match else "",
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    await redis.zadd(PO_REMINDERS_KEY, {reminder: fire_at})

    logger.info("po_reminder_set", telegram_chat_id=telegram_chat_id, delay_minutes=delay_minutes)
    return f"Reminder set for {delay_minutes} minutes: {reason}"


@tool
async def notify_user(message: str, *, config: RunnableConfig) -> str:
    """Send an intermediate message to the user and continue working.

    Use this ONLY when you need to send a progress update while continuing
    to use more tools. For example: "Setting up your project..." before calling
    create_story. Your final response is always delivered to the user
    automatically — do NOT use this tool for final replies.

    Args:
        message: Text to send to the user right now.
    """
    client = _get_stream_client()
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    msg = POProactiveMessage(text=message, telegram_chat_id=telegram_chat_id)
    await client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(msg))

    logger.info("po_notify_user", telegram_chat_id=telegram_chat_id, text_length=len(message))
    return "Message sent to user."


@tool
async def get_budget_balance(*, config: RunnableConfig) -> str:
    """Read the current user's engineering spend and available budget.

    Call this to answer budget questions and immediately before creating or
    reopening a story. The returned remaining amount already accounts for all
    internal holds; do not recalculate it.
    """
    response = await _get_api().get_raw(
        "engineering-budget-policy/balance",
        headers=_user_headers(config),
    )
    response.raise_for_status()
    data = response.json()
    policy = data.get("policy") or {}

    fields = [
        f"enforcement={data['enforcement']}",
        (
            f"known_spend_microusd={data['known_spend_microusd']} "
            f"({format_microusd(data['known_spend_microusd'])})"
        ),
    ]
    remaining = data["remaining_microusd"]
    if remaining is None:
        fields.append("remaining_microusd=null (no enforced finite limit)")
    else:
        fields.append(f"remaining_microusd={remaining} ({format_microusd(remaining)})")
    reservation = policy.get("attempt_reservation_microusd")
    if reservation is not None:
        fields.append(
            f"attempt_reservation_microusd={reservation} ({format_microusd(reservation)})"
        )
    fields.extend(
        [
            f"exhausted={str(data['exhausted']).lower()}",
            f"unknown_cost_attempt_count={data['unknown_cost_attempt_count']}",
            f"incomplete_coverage={str(data['incomplete_coverage']).lower()}",
        ]
    )
    return "\n".join(fields)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo.

    Use this to find documentation for third-party APIs or services
    when the user's project needs to integrate with an external service.

    Args:
        query: Search query (e.g. "OpenWeatherMap API documentation").
        max_results: Maximum number of results to return (default 5).
    """
    from ddgs import DDGS

    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:
        logger.warning("web_search_failed", query=query, error=str(exc))
        return f"Search failed: {exc}"

    if not results:
        return f"No results found for: {query}"

    lines = []
    for r in results:
        lines.append(f"<b>{r['title']}</b>")
        lines.append(f"{r['body']}")
        lines.append(f"URL: {r['href']}")
        lines.append("")
    return "\n".join(lines).strip()


def get_all_tools() -> list:
    """Return all PO tools for the ReactAgent."""
    return [
        create_project,
        list_projects,
        get_project,
        set_bot_access,
        add_bot_user,
        remove_bot_user,
        set_project_secret,
        teardown_project,
        validate_telegram_token,
        create_story,
        list_stories,
        reopen_story,
        get_story,
        get_run_status,
        get_budget_balance,
        set_reminder,
        notify_user,
        web_search,
    ]


__all__ = [
    # Shared helpers
    "init_po_clients",
    "_get_api",
    "_get_stream_client",
    "_user_headers",
    # Project tools
    "AVAILABLE_MODULES",
    "HTTP_UNPROCESSABLE",
    "add_bot_user",
    "create_project",
    "list_projects",
    "get_project",
    "set_bot_access",
    "remove_bot_user",
    "set_project_secret",
    "teardown_project",
    "validate_telegram_token",
    # Story/run tools
    "create_story",
    "list_stories",
    "reopen_story",
    "get_story",
    "get_run_status",
    "get_budget_balance",
    # Utility tools
    "set_reminder",
    "notify_user",
    "web_search",
    "get_all_tools",
    # Constants
    "PO_REMINDERS_KEY",
]
