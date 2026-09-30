"""The PO's situation snapshot: what is true now, read by code, not remembered by the model.

A system event can reach the PO long after the fact it reports: a ``story_blocked``
owed for three weeks reads exactly like one raised a minute ago. So every system
event turn carries this snapshot, and on request ``get_product_situation`` answers
with the same text. It says, each with an absolute UTC date and a human age:

* the order — the story and when its Product Brief was confirmed, or that it is
  not an order;
* the story's status, when it entered it (``status_entered_at``), for a
  stopped story the fixed fact ``STOPPED_FACT`` and, for a story the state-age
  watchdog stopped, the wait it had exceeded (``MASS_SWEEP_MARK`` when that
  pass was a mass sweep after downtime);
* when the user last wrote in this chat;
* whether the project's deployed application is up;
* the user's other ordered stories in work, and a count of platform work;
* deferred notices (``read_deferred_notices``), until explicitly told or closed.

It is total: every field is read on its own, and a read that raises or returns
something unexpected makes that one field ``unknown``. Nothing here decides who
hears about a story — that stays with the consumer's fail-closed audience check.

The snapshot is model input for one turn and never a chat message: the consumer
passes it in the run config (``SITUATION_CONFIG_KEY``) and the graph's prompt
appends it to the system message, so the checkpointer never stores it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Protocol

from pydantic import TypeAdapter
import structlog

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.application import ApplicationDTO, ApplicationStatus
from shared.contracts.dto.owner_notification import AddressedOwnerNotice
from shared.contracts.dto.product_brief import ProductBriefRead
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.state_wait import STATE_AGE_BOUND_REASON, StateWaitExpiryReason
from shared.contracts.dto.story import STAGE_NOTICE_TERMINAL_STATUSES, StoryDTO, StoryType

from ...consumers.po_story_gate import StoryKeyState, story_key_state

logger = structlog.get_logger(__name__)

#: The run-config key the consumer puts a system event's snapshot under.
SITUATION_CONFIG_KEY = "po_situation"

SNAPSHOT_HEADING = "## Situation snapshot"
DEFERRED_NOTICES_HEADING = "### Deferred notices"
UNKNOWN = "unknown"
#: What a stopped story is, told as a fact and never softened: nobody is
#: working on it and nothing promises when that changes.
STOPPED_FACT = "stopped, a person is needed, no known deadline"
#: A watchdog ending from a pass that ended many waits at once after downtime.
MASS_SWEEP_MARK = "stopped in a mass sweep after downtime"

#: When the user last wrote in a chat, written on every user turn (ISO, UTC).
LAST_USER_MESSAGE_KEY_PREFIX = "po:last_user_message:"

_UP_STATUSES = {ApplicationStatus.RUNNING: "up", ApplicationStatus.DEGRADED: "degraded"}


class SituationReader(Protocol):
    """The existing API reads the snapshot is built from."""

    async def list_deferred_notices(self, project_id: str) -> list[AddressedOwnerNotice]: ...

    async def get_story(self, story_id: str) -> StoryDTO: ...

    async def get_product_brief_by_story(self, story_id: str) -> ProductBriefRead | None: ...

    async def list_owned_projects(self, telegram_id: int) -> list[ProjectDTO]: ...

    async def list_project_stories(self, project_id: str) -> list[StoryDTO]: ...

    async def list_repositories(self, project_id: str) -> list[RepositoryDTO]: ...

    async def list_applications(self, repo_id: str) -> list[ApplicationDTO]: ...


class ApiSituationReader:
    """``SituationReader`` over the internal API: existing endpoints, validated DTOs.

    Built on the generic client, so the consumer's ``LanggraphAPIClient`` and the
    plain client a harness hands the PO tools read the same way. A body that is
    not the DTO raises, and the snapshot shows that field as unknown.
    """

    def __init__(self, api: InternalAPIClient) -> None:
        self._api = api

    async def _json(self, path: str, **kwargs) -> object:
        return (await self._api.request("GET", path, **kwargs)).json()

    async def list_deferred_notices(self, project_id: str) -> list[AddressedOwnerNotice]:
        return TypeAdapter(list[AddressedOwnerNotice]).validate_python(
            await self._json(
                "stories/owner-notifications/deferred", params={"project_id": project_id}
            )
        )

    async def get_story(self, story_id: str) -> StoryDTO:
        return StoryDTO.model_validate(await self._json(f"stories/{story_id}"))

    async def get_product_brief_by_story(self, story_id: str) -> ProductBriefRead | None:
        response = await self._api.get_raw(f"product-briefs/by-story/{story_id}")
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        response.raise_for_status()
        return ProductBriefRead.model_validate(response.json())

    async def list_owned_projects(self, telegram_id: int) -> list[ProjectDTO]:
        rows = await self._json(
            "projects/",
            params={"owner_only": "true"},
            headers={"X-Telegram-ID": str(telegram_id)},
        )
        return _PROJECTS.validate_python(rows)

    async def list_project_stories(self, project_id: str) -> list[StoryDTO]:
        return _STORIES.validate_python(
            await self._json("stories/", params={"project_id": project_id})
        )

    async def list_repositories(self, project_id: str) -> list[RepositoryDTO]:
        return _REPOSITORIES.validate_python(
            await self._json("repositories/", params={"project_id": project_id})
        )

    async def list_applications(self, repo_id: str) -> list[ApplicationDTO]:
        return _APPLICATIONS.validate_python(
            await self._json("applications/", params={"repo_id": repo_id})
        )


_PROJECTS = TypeAdapter(list[ProjectDTO])
_STORIES = TypeAdapter(list[StoryDTO])
_REPOSITORIES = TypeAdapter(list[RepositoryDTO])
_APPLICATIONS = TypeAdapter(list[ApplicationDTO])


class KeyValueStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> object: ...


def last_user_message_key(telegram_chat_id: str) -> str:
    return f"{LAST_USER_MESSAGE_KEY_PREFIX}{telegram_chat_id}"


async def record_user_message(redis: KeyValueStore, telegram_chat_id: str, at: datetime) -> None:
    """Remember that the user wrote in this chat at ``at``; the snapshot reads it back."""
    await redis.set(last_user_message_key(telegram_chat_id), at.astimezone(UTC).isoformat())


async def read_deferred_notices(
    telegram_chat_id: str,
    project_id: str,
    *,
    reader: SituationReader,
    projects: list[ProjectDTO] | None = None,
) -> list[str]:
    """All suppressed notices of this user's projects, without an age cutoff."""
    if projects is None:
        projects = await reader.list_owned_projects(int(telegram_chat_id))
    notices = []
    for project in projects:
        # The focus project never hides deferred notices in another owned project.
        for item in await reader.list_deferred_notices(str(project.id)):
            record = item.notification
            notices.append(
                f"story={record.story_id} event={record.event}: {record.text}; "
                f"reason={record.suppressed_reason}; decided by={record.suppressed_by}; "
                f"deferred at={record.suppressed_at.isoformat()}"
            )
    return notices


def human_age(moment: datetime, now: datetime) -> str:
    """How long ago ``moment`` was, in the words a person would use: ``3 weeks ago``."""
    seconds = (now - _utc(moment)).total_seconds()
    if seconds < 0:
        return "in the future"
    if seconds < 60:  # noqa: PLR2004
        return "just now"
    return f"{human_duration(seconds)} ago"


def human_duration(seconds: float) -> str:
    """A span of time in the words a person would use: ``3 weeks``."""
    minutes = max(int(seconds // 60), 0)
    hours, days = minutes // 60, minutes // 1440
    if minutes < 60:  # noqa: PLR2004 — the unit boundaries are the wording
        return _span(minutes, "minute")
    if hours < 24:  # noqa: PLR2004
        return _span(hours, "hour")
    if days < 14:  # noqa: PLR2004
        return _span(days, "day")
    if days < 63:  # noqa: PLR2004
        return _span(days // 7, "week")
    if days < 365:  # noqa: PLR2004
        return _span(days // 30, "month")
    return _span(days // 365, "year")


def _span(amount: int, unit: str) -> str:
    return f"{amount} {unit}{'' if amount == 1 else 's'}"


def when(moment: datetime, now: datetime) -> str:
    """An absolute UTC date and its human age: ``2026-09-06 10:00 UTC (3 weeks ago)``."""
    return f"{_utc(moment):%Y-%m-%d %H:%M} UTC ({human_age(moment, now)})"


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _is_ordered(brief: ProductBriefRead | None) -> bool:
    return brief is not None and brief.confirmed_at is not None


def _in_work(story: StoryDTO) -> bool:
    return story.status not in STAGE_NOTICE_TERMINAL_STATUSES


@dataclass(frozen=True)
class ProjectWork:
    """A project's stories in work, split by who they are for."""

    ordered: list[StoryDTO]
    platform: int


@dataclass(frozen=True)
class SituationSubject:
    """What a snapshot is about: a chat, and the project and story an event names."""

    telegram_chat_id: str
    project_id: str = ""
    story_id: str = ""


class _Snapshot:
    """One snapshot's reads, each memoized so two fields never read one source twice."""

    def __init__(
        self,
        reader: SituationReader,
        redis: KeyValueStore,
        subject: SituationSubject,
        now: datetime,
    ) -> None:
        self._reader = reader
        self._redis = redis
        self.subject = subject
        self.now = now
        self._memo: dict[object, asyncio.Future] = {}

    async def _once(self, key: object, read: Callable[[], Awaitable[object]]) -> object:
        # A task, not a result: concurrent fields share one read in flight. A read
        # that fails is its field's ``unknown``; its task never logs as unretrieved.
        if key not in self._memo:
            task = asyncio.ensure_future(read())
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            self._memo[key] = task
        return await self._memo[key]

    async def story(self, story_id: str) -> StoryDTO:
        return await self._once(("story", story_id), lambda: self._reader.get_story(story_id))

    async def brief(self, story_id: str) -> ProductBriefRead | None:
        return await self._once(
            ("brief", story_id), lambda: self._reader.get_product_brief_by_story(story_id)
        )

    async def owned_projects(self) -> list[ProjectDTO]:
        telegram_id = int(self.subject.telegram_chat_id)
        return await self._once("projects", lambda: self._reader.list_owned_projects(telegram_id))

    async def project_stories(self, project_id: str) -> list[StoryDTO]:
        return await self._once(
            ("stories", project_id), lambda: self._reader.list_project_stories(project_id)
        )

    async def project_work(self, project_id: str) -> ProjectWork:
        stories = [s for s in await self.project_stories(project_id) if _in_work(s)]
        briefs = await asyncio.gather(*(self.brief(s.id) for s in stories))
        ordered = [
            story
            for story, brief in zip(stories, briefs, strict=True)
            if story.type is not StoryType.TECHNICAL and _is_ordered(brief)
        ]
        return ProjectWork(ordered=ordered, platform=len(stories) - len(ordered))

    async def focus_project_id(self) -> str:
        if self.subject.project_id:
            return self.subject.project_id
        if not self.subject.story_id:
            raise LookupError("the event names no project")
        return str((await self.story(self.subject.story_id)).project_id)

    async def latest_ordered_story(self, project_id: str) -> StoryDTO | None:
        """The project's current ordered story, or its latest one when none is in work."""
        stories = sorted(
            await self.project_stories(project_id), key=lambda s: s.created_at, reverse=True
        )
        for candidates in ([s for s in stories if _in_work(s)], stories):
            for story in candidates:
                if story.type is not StoryType.TECHNICAL and _is_ordered(
                    await self.brief(story.id)
                ):
                    return story
        return None

    async def last_user_message(self) -> datetime | None:
        raw = await self._redis.get(last_user_message_key(self.subject.telegram_chat_id))
        if raw is None:
            return None
        if not isinstance(raw, str | bytes):
            raise TypeError(f"unexpected last-message value {type(raw).__name__}")
        return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)

    async def applications(self, project_id: str) -> list[ApplicationDTO]:
        repositories = await self._reader.list_repositories(project_id)
        found: list[ApplicationDTO] = []
        for repository in repositories:
            found += await self._reader.list_applications(repository.id)
        return found


async def _field(name: str, render: Callable[[], Awaitable[str]]) -> str:
    """One field's text, or ``unknown`` when any read behind it failed."""
    try:
        return await render()
    except Exception as exc:  # noqa: BLE001 — totality: one source never fails the turn
        _log_unknown(name, exc)
        return UNKNOWN


def _log_unknown(name: str, exc: Exception) -> None:
    logger.warning(
        "po_situation_field_unknown",
        field=name,
        error_type=type(exc).__name__,
        error=str(exc)[:300],
    )


def _story_label(story: StoryDTO) -> str:
    return f'{story.id} "{story.title}"'


async def build_situation(
    reader: SituationReader,
    redis: KeyValueStore,
    subject: SituationSubject,
    *,
    now: datetime | None = None,
    requested: bool = False,
) -> str:
    """The snapshot text for ``subject``; never raises for a source that fails.

    ``requested`` is the ``get_product_situation`` form: without a story named,
    the story part is about the project's current or latest ordered story.
    """
    snap = _Snapshot(reader, redis, subject, now or datetime.now(UTC))
    story_id = subject.story_id
    if story_id:
        story_lines = await _story_lines(snap, story_id)
    elif requested:
        story_lines, story_id = await _requested_story_lines(snap)
    else:
        story_lines = ["- Story: none named by this event"]

    last_message = await _field("last_user_message", lambda: _render_last_message(snap))
    application = await _field("application", lambda: _render_application(snap))
    others = await _field("other_ordered_stories", lambda: _render_other_orders(snap, story_id))
    platform = await _field("platform_work", lambda: _render_platform_work(snap))
    deferred = await _field("deferred_notices", lambda: _render_deferred(snap))

    occasion = "requested" if requested else "built for this system event"
    return "\n".join(
        [
            f"{SNAPSHOT_HEADING} ({occasion}, {snap.now:%Y-%m-%d %H:%M} UTC)",
            "Read by code just now. It is not a message from the user and not stored in "
            "the chat; tell an old event by these dates.",
            *story_lines,
            f"- User's last message in this chat: {last_message}",
            f"- Application: {application}",
            f"- Other ordered stories in work: {others}",
            f"- Platform work in this project: {platform}",
            "",
            DEFERRED_NOTICES_HEADING,
            deferred,
        ]
    )


async def _requested_story_lines(snap: _Snapshot) -> tuple[list[str], str]:
    """The story part for the project's current or latest ordered story, and its id."""
    try:
        story = await snap.latest_ordered_story(await snap.focus_project_id())
    except Exception as exc:  # noqa: BLE001 — totality, as in ``_field``
        _log_unknown("story", exc)
        return [f"- Story: {UNKNOWN}"], ""
    if story is None:
        return ["- Story: no ordered story in this project"], ""
    return await _story_lines(snap, story.id), story.id


async def _story_lines(snap: _Snapshot, story_id: str) -> list[str]:
    async def title() -> str:
        return _story_label(await snap.story(story_id))

    async def order() -> str:
        brief = await snap.brief(story_id)
        if not _is_ordered(brief):
            return "not an order (no confirmed Product Brief)"
        return f"ordered, Product Brief confirmed {when(brief.confirmed_at, snap.now)}"

    async def status() -> str:
        story = await snap.story(story_id)
        waiting = f", waiting on {story.waiting_on}" if story.waiting_on != "none" else ""
        stopped = f"; {STOPPED_FACT}" if story_key_state(story) is StoryKeyState.STOPPED else ""
        return f"{story.status}{waiting}{stopped}{_prior_wait(story)}"

    async def since() -> str:
        return _entered(await snap.story(story_id), snap.now)

    return [
        f"- Story: {await _field('story', title)}",
        f"- Order: {await _field('order', order)}",
        f"- Status: {await _field('status', status)}",
        f"- In this status since: {await _field('status_entered_at', since)}",
    ]


def _entered(story: StoryDTO, now: datetime) -> str:
    """When the story landed on its status; a row landed before that was recorded raises.

    Never ``updated_at``: unrelated writes (title, quarantine metadata, notices)
    move it, and a three-week wait would read as a minute old.
    """
    if story.status_entered_at is None:
        raise LookupError(f"{story.id} has no recorded status entry time")
    return when(story.status_entered_at, now)


def _prior_wait(story: StoryDTO) -> str:
    """The wait the state-age watchdog ended, as its recorded reason states it, or nothing.

    A fresh park after a long wait is the case an old event hides in: the story
    entered ``waiting_human_review`` minutes ago, after weeks in ``pr_review``.
    """
    reason = story.quarantine_reason
    if not isinstance(reason, dict) or reason.get("reason") != STATE_AGE_BOUND_REASON:
        return ""
    ended = StateWaitExpiryReason.model_validate(reason)
    began = _utc(datetime.fromisoformat(ended.anchor_at))
    mass = f"; {MASS_SWEEP_MARK}" if ended.mass_sweep else ""
    return (
        f"{mass}; stopped after waiting {human_duration(ended.age_minutes * 60)} in {ended.status}"
        f" (that wait began {began:%Y-%m-%d %H:%M} UTC)"
    )


async def _render_last_message(snap: _Snapshot) -> str:
    moment = await snap.last_user_message()
    return "none recorded" if moment is None else when(moment, snap.now)


async def _render_application(snap: _Snapshot) -> str:
    deployed = [
        app
        for app in await snap.applications(await snap.focus_project_id())
        if app.status is not ApplicationStatus.NOT_DEPLOYED
    ]
    if not deployed:
        return "not deployed"
    parts = []
    for app in deployed:
        health = (
            f"last health check {when(app.last_health_check, snap.now)}"
            if app.last_health_check
            else "no health check recorded"
        )
        state = _UP_STATUSES.get(app.status, "not up")
        parts.append(f"{app.service_name}: {app.status} ({state}), {health}")
    return "; ".join(parts)


async def _render_other_orders(snap: _Snapshot, focus_story_id: str) -> str:
    projects = await snap.owned_projects()
    works = await asyncio.gather(*(snap.project_work(str(p.id)) for p in projects))
    lines = [
        f"\n  - {_story_label(story)} (project {project.title}): {story.status}, "
        f"in it since {_entered_or_unknown(story, snap.now)}"
        for project, work in zip(projects, works, strict=True)
        for story in work.ordered
        if story.id != focus_story_id
    ]
    return "".join(lines) if lines else "none"


def _entered_or_unknown(story: StoryDTO, now: datetime) -> str:
    return UNKNOWN if story.status_entered_at is None else when(story.status_entered_at, now)


async def _render_platform_work(snap: _Snapshot) -> str:
    work = await snap.project_work(await snap.focus_project_id())
    noun = "story" if work.platform == 1 else "stories"
    return f"{work.platform} {noun} in work (not ordered, or technical)"


async def _render_deferred(snap: _Snapshot) -> str:
    project_id = snap.subject.project_id
    notices = await read_deferred_notices(
        snap.subject.telegram_chat_id,
        project_id,
        reader=snap._reader,
        projects=await snap.owned_projects(),
    )
    return "\n".join(f"- {notice}" for notice in notices) if notices else "none"
