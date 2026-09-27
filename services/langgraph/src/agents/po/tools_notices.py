"""PO decisions about durable notices and their single publication check."""

import json
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import TypeAdapter, ValidationError
import structlog

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.owner_notification import (
    AddressedOwnerNotice,
    OwnerNoticeReference,
    OwnerNoticeSettlement,
)
from shared.contracts.queues.po import POSystemEvent

from .situation import ApiSituationReader
from .tools_shared import _get_api, _get_stream_client

logger = structlog.get_logger(__name__)
NOTICE_LIST = TypeAdapter(list[AddressedOwnerNotice])


class OwnerNoticeReadUnknown(RuntimeError):
    """The event must remain pending until the publication decision can be read."""


def latest_notice_key(chat: str, story: str) -> str:
    return f"po:latest_owner_event:{chat}:{story}"


async def remember_owner_event(redis, chat: str, data: dict) -> None:
    if data.get("type") != "system_event" or not data.get("story_id"):
        return
    try:
        await redis.set(
            latest_notice_key(chat, data["story_id"]),
            POSystemEvent.model_validate(data).model_dump_json(),
        )
    except Exception as exc:
        raise OwnerNoticeReadUnknown(data["story_id"]) from exc


async def read_notices(api: InternalAPIClient, story_id: str) -> list[AddressedOwnerNotice]:
    response = await api.get_raw(f"stories/{story_id}/owner-notifications")
    response.raise_for_status()
    return NOTICE_LIST.validate_python(response.json())


async def _owns_story(story_id: str, config: RunnableConfig) -> bool:
    reader = ApiSituationReader(_get_api())
    story = await reader.get_story(story_id)
    projects = await reader.list_owned_projects(int(config["configurable"]["telegram_chat_id"]))
    return str(story.project_id) in {str(project.id) for project in projects}


async def _settle(story_id: str, command: OwnerNoticeSettlement) -> str:
    response = await _get_api().post_raw(
        f"stories/{story_id}/owner-notifications/settlement", json=command.model_dump(mode="json")
    )
    if not response.is_success:
        return f"Notice was not settled: {response.json()['detail']}"
    return f"Notice recorded as {command.told_state}."


@tool
async def suppress_owner_notice(
    story_id: str, reason: str, decided_by: Literal["po", "user"] = "po", *, config: RunnableConfig
) -> str:
    """Defer the latest delivered, unsettled notice; admins receive its text and your reason.

    Use po for your judgement, user for the user's request. Never suppress a
    request for a user secret. A notice without a durable record must be told
    or noted to admins. Deferral changes no product facts.
    """
    if not reason.strip():
        return "A non-empty reason is required to defer a notice."
    if not await _owns_story(story_id, config):
        return "This story is not among the user's projects."
    brief = await ApiSituationReader(_get_api()).get_product_brief_by_story(story_id)
    if brief is None or brief.confirmed_at is None:
        return "This is not an ordered story; note the service matter to the admins."
    chat = config["configurable"]["telegram_chat_id"]
    latest = await _get_stream_client().redis.get(latest_notice_key(chat, story_id))
    if latest:
        event = POSystemEvent.model_validate_json(latest)
        if event.owner_notice is None:
            return "The latest event has no durable record: tell it, or note it to the admins."
    notices = await read_notices(_get_api(), story_id)
    delivered = [n for n in notices if n.notification.state == "delivered"]
    if not delivered or delivered[0].notification.told_state is not None:
        return "No delivered, unsettled notice; read get_product_situation for deferred notices."
    notice = delivered[0]
    if latest and event.owner_notice != OwnerNoticeReference(
        source=notice.source,
        source_id=notice.source_id,
        owed_at=notice.owed_at,
    ):
        return "The latest event's record is not delivered yet; read it again before deferring."
    try:
        command = OwnerNoticeSettlement(
            source=notice.source,
            source_id=notice.source_id,
            owed_at=notice.owed_at,
            told_state="suppressed",
            reason=reason,
            suppressed_by=decided_by,
        )
    except ValidationError as exc:
        return f"Notice was not deferred: {exc}"
    return await _settle(story_id, command)


@tool
async def resolve_deferred_notice(
    story_id: str, outcome: Literal["told", "closed"], reason: str = "", *, config: RunnableConfig
) -> str:
    """Resolve this story's oldest deferred notice after telling it or an explicit drop request.

    Tell deferred notices first in a user turn, then mark each told. Close only
    when the user or an admin said to drop it, with their reason. Nothing expires.
    """
    if outcome == "told" and not config["configurable"].get("user_turn", False):
        return "Tell deferred notices in a user turn before marking them told."
    if outcome == "closed" and not reason.strip():
        return "Closing a deferred notice requires the user's or admin's reason."
    if not await _owns_story(story_id, config):
        return "This story is not among the user's projects."
    notices = await read_notices(_get_api(), story_id)
    deferred = sorted(
        (n for n in notices if n.notification.told_state == "suppressed"), key=lambda n: n.owed_at
    )
    if not deferred:
        return "No deferred notice for this story."
    notice = deferred[0]
    return await _settle(
        story_id,
        OwnerNoticeSettlement(
            source=notice.source,
            source_id=notice.source_id,
            owed_at=notice.owed_at,
            told_state=outcome,
            resolve_deferred=True,
            reason=reason,
        ),
    )


def event_reference(data: dict) -> OwnerNoticeReference | None:
    raw = data.get("owner_notice")
    if raw is None:
        return None
    return OwnerNoticeReference.model_validate(json.loads(raw) if isinstance(raw, str) else raw)


async def notice_may_publish(api: InternalAPIClient, data: dict) -> bool:
    reference = event_reference(data)
    if reference is None:
        return True
    try:
        notices = await read_notices(api, data["story_id"])
        return any(
            n.source == reference.source
            and n.source_id == reference.source_id
            and n.owed_at == reference.owed_at
            and n.notification.told_state is None
            for n in notices
        )
    except Exception as exc:
        logger.warning("po_owner_notice_read_failed", story_id=data["story_id"], error=str(exc))
        raise OwnerNoticeReadUnknown(data["story_id"]) from exc


async def record_notice_told(api: InternalAPIClient, data: dict) -> None:
    reference = event_reference(data)
    if reference is None:
        return
    try:
        command = OwnerNoticeSettlement(**reference.model_dump(), told_state="told")
        response = await api.post_raw(
            f"stories/{data['story_id']}/owner-notifications/settlement",
            json=command.model_dump(mode="json"),
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "po_owner_notice_told_write_failed", story_id=data["story_id"], error=str(exc)
        )
