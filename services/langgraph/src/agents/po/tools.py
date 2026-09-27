"""PO ReactAgent tools.

Async tools for the Product Owner agent. Uses the shared internal API client and
the Redis client, both initialized at consumer startup via tools_shared.init_po_clients().

get_all_tools() composes the agent tool list. Import domain tools and constants
from their owner modules; this module owns only the utility tools below.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import time

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared import queues
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.engineering_budget_display import format_microusd
from shared.notifications import AdminDeliveryStatus, deliver_to_admins

from . import tools_briefs, tools_projects, tools_shared, tools_stories

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tools that live in this module (utility / non-domain)
# ---------------------------------------------------------------------------


@tool
async def set_reminder(
    delay_minutes: int, reason: str, story_id: str | None = None, *, config: RunnableConfig
) -> str:
    """Set a reminder to wake up after a delay and re-check something.

    Use when the user asks to be reminded or a later check is needed.
    Do not set progress reminders after creating a story.

    A reminder is for you to re-check, not a scheduled message to the user.
    A story reminder can tell only an untold need for the user or a stop.
    In-work stories get no reply; endings come from durable events.
    A reminder about no story is answered only if the user asked for it.

    Args:
        delay_minutes: Minutes until reminder fires.
        reason: Why you're setting this reminder (e.g. "re-check the payment story").
        story_id: The story it concerns (story-...), if any.
    """
    redis = tools_shared._get_stream_client().redis
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    fire_at = time.time() + delay_minutes * 60

    reminder = json.dumps(
        {
            "type": "reminder",
            "telegram_chat_id": telegram_chat_id,
            "text": reason,
            "story_id": story_id or "",
            # Set in the user's own turn: the user asked for it, so its reply
            # may reach them even when it names no story.
            "user_requested": bool(config["configurable"].get("user_turn")),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    await redis.zadd(queues.PO_REMINDERS_KEY, {reminder: fire_at})

    logger.info(
        "po_reminder_set",
        telegram_chat_id=telegram_chat_id,
        delay_minutes=delay_minutes,
        story_id=story_id or "",
    )
    return f"Reminder set for {delay_minutes} minutes: {reason}"


#: The tool result a non-user turn gets from ``notify_user``: nothing was sent.
_NOTIFY_USER_REFUSED = (
    "Not sent: in reminder/system turns only your final reply reaches the user, "
    "and it is sent only if the story changed."
)


@tool
async def notify_user(message: str, *, config: RunnableConfig) -> str:
    """Send an intermediate message to the user and continue working.

    Use this ONLY when you need to send a progress update while continuing
    to use more tools. For example: "Setting up your project..." before calling
    create_story. Your final response is always delivered to the user
    automatically — do NOT use this tool for final replies.

    Only in a turn answering the user. In a reminder or system turn nothing is
    sent: your final reply is the only way to reach the user there.

    Args:
        message: Text to send to the user right now.
    """
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    if not config["configurable"]["user_turn"]:
        # A reminder or system turn reaches the user only through its gated
        # final reply (`consumers/po_story_gate.py`); a direct publish here
        # would bypass it and could repeat an unchanged story without end.
        logger.info("po_notify_user_refused", telegram_chat_id=telegram_chat_id)
        return _NOTIFY_USER_REFUSED
    client = tools_shared._get_stream_client()
    msg = POProactiveMessage(text=message, telegram_chat_id=telegram_chat_id)
    await client.publish_flat(queues.PO_PROACTIVE_QUEUE, to_flat_fields(msg))

    logger.info("po_notify_user", telegram_chat_id=telegram_chat_id, text_length=len(message))
    return "Message sent to user."


@tool
async def note_to_admins(text: str, *, config: RunnableConfig) -> str:
    """Send a note to the platform's admins. Starts no work, sends the user nothing.

    Use it for a service matter you notice in conversation that is not the
    user's order: a platform problem, a tool that misbehaves, something only
    an operator can decide. Never create or reopen a story for such a matter.

    Args:
        text: What the admins should know, in plain words, with the project
            or story id when there is one.
    """
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    user_name = config["configurable"].get("user_name", "")
    result = await deliver_to_admins(
        f"PO note (chat={telegram_chat_id} user={user_name or '-'}): {text}", level="info"
    )
    logger.info(
        "po_note_to_admins",
        telegram_chat_id=telegram_chat_id,
        delivery=result.status.value,
        text_length=len(text),
    )
    if result.status is AdminDeliveryStatus.DELIVERED:
        return "Note delivered to the admins. No work was started."
    return f"The note did not reach every admin ({result.detail}). No work was started."


@tool
async def get_budget_balance(*, config: RunnableConfig) -> str:
    """Read the current user's engineering spend and available budget.

    Call this to answer budget questions and immediately before creating or
    reopening a story. The returned remaining amount already accounts for all
    internal holds; do not recalculate it.
    """
    response = await tools_shared._get_api().get_raw(
        "engineering-budget-policy/balance",
        headers=tools_shared._user_headers(config),
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
        tools_projects.create_project,
        tools_projects.list_projects,
        tools_projects.get_project,
        tools_projects.grant_project_user,
        tools_projects.set_project_secret,
        tools_projects.transfer_project_ownership,
        tools_projects.teardown_project,
        tools_projects.validate_telegram_token,
        tools_briefs.present_product_brief,
        tools_briefs.confirm_product_brief,
        tools_briefs.show_full_brief,
        tools_stories.create_story,
        tools_stories.list_stories,
        tools_stories.reopen_story,
        tools_stories.get_story,
        tools_stories.get_product_situation,
        tools_stories.record_unverified_decision,
        tools_stories.get_story_diagnostics,
        tools_stories.get_run_status,
        get_budget_balance,
        set_reminder,
        notify_user,
        note_to_admins,
        web_search,
    ]


__all__ = [
    "get_all_tools",
    "get_budget_balance",
    "note_to_admins",
    "notify_user",
    "set_reminder",
    "web_search",
]
