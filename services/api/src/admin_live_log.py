"""The live agent log of one story or one worker, read back from Loki.

A story's lines are the ``agent_log_line`` events keyed by its planning attempt ids and its
engineering and QA Run ids; a worker's lines are those carrying its Loki label ``worker_id``.
"""

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import os
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.loki import LokiClient
from shared.contracts.dto.agent_log import AGENT_LOG_LINE_EVENT, AgentLogLine
from shared.contracts.dto.run import RunType
from shared.models import ProductBrief, RequirementCoverage, Run, Story, Task

from .schemas.admin_console import (
    LiveLogLine,
    LiveLogResponse,
    LiveLogSource,
    LiveLogUnavailable,
)
from .schemas.utc import assume_utc
from .story_diagnostics_logs import LOKI_READ_TIMEOUT_SECONDS, safe_log_id

logger = structlog.get_logger()

#: The newest lines one live log read returns.
LIVE_LOG_LINE_LIMIT = 1000

#: The most attempt ids one story query names; the newest by creation time are kept.
LIVE_LOG_ATTEMPT_ID_CAP = 64

#: How far back a worker read looks: the log store's retention period.
WORKER_LIVE_LOG_LOOKBACK = timedelta(hours=168)

_RUN_SOURCE = {
    RunType.ENGINEERING.value: LiveLogSource.DEVELOPER,
    RunType.QA.value: LiveLogSource.QA,
}

_AGENT_LOG_PIPELINE = f'|= "{AGENT_LOG_LINE_EVENT}" | json | event="{AGENT_LOG_LINE_EVENT}"'


def story_live_log_query(attempt_ids: Iterable[str]) -> str:
    """The LogQL selecting ``agent_log_line`` events of exactly ``attempt_ids``, any container."""
    ids = sorted({safe_log_id(attempt_id) for attempt_id in attempt_ids})
    if not ids:
        raise ValueError("a story live log query needs at least one attempt id")
    return f'{{job="docker"}} {_AGENT_LOG_PIPELINE} | attempt_id=~"{"|".join(ids)}"'


def worker_live_log_query(worker_id: str) -> str:
    """The LogQL selecting ``agent_log_line`` events of the container labelled ``worker_id``."""
    return f'{{worker_id="{safe_log_id(worker_id)}"}} {_AGENT_LOG_PIPELINE}'


def story_attempt_sources(
    brief: Any | None, tasks: Iterable[Any], coverages: Iterable[Any], runs: Iterable[Any]
) -> tuple[dict[str, LiveLogSource], int]:
    """The story's newest ``LIVE_LOG_ATTEMPT_ID_CAP`` attempt ids by source, and how many fell off.

    A planning attempt is as old as the first row that carries it.
    """
    sources: dict[str, LiveLogSource] = {}
    created: dict[str, datetime] = {}

    def note(attempt_id: str, source: LiveLogSource, at: datetime) -> None:
        sources[attempt_id] = source
        at = assume_utc(at)
        created[attempt_id] = min(created.get(attempt_id, at), at)

    if brief is not None and brief.planning_attempt_id is not None:
        note(brief.planning_attempt_id, LiveLogSource.ARCHITECT, brief.updated_at)
    for row in (*tasks, *coverages):
        if row.planning_attempt_id is not None:
            note(row.planning_attempt_id, LiveLogSource.ARCHITECT, row.created_at)
    for run in runs:
        if run.type in _RUN_SOURCE:
            note(run.id, _RUN_SOURCE[run.type], run.created_at)
    newest = sorted(sources, key=lambda attempt_id: (created[attempt_id], attempt_id), reverse=True)
    kept = newest[:LIVE_LOG_ATTEMPT_ID_CAP]
    return {attempt_id: sources[attempt_id] for attempt_id in kept}, len(newest) - len(kept)


def _loki_time(nanoseconds: int) -> datetime:
    seconds, rest = divmod(nanoseconds, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=rest // 1000)


def live_log_lines(
    entries: Iterable[dict], sources: Mapping[str, LiveLogSource]
) -> list[LiveLogLine]:
    """Loki entries as console lines, oldest first, the lines of one attempt in ``seq`` order."""
    lines = []
    for entry in entries:
        fields = {name: entry[name] for name in AgentLogLine.model_fields}
        line = AgentLogLine.model_validate(fields)
        lines.append(
            LiveLogLine(
                at=_loki_time(entry["_ts"]),
                source=sources[line.attempt_id],
                attempt_id=line.attempt_id,
                seq=line.seq,
                kind=line.kind,
                text=line.text,
            )
        )
    lines.sort(key=lambda line: (line.at, line.attempt_id, line.seq))
    # Producers stamp lines in different containers, so one attempt's Loki times may tie or
    # disagree with its seq; each attempt's slots are refilled in seq order.
    slots: dict[str, list[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        slots[line.attempt_id].append(index)
    ordered = list(lines)
    for indexes in slots.values():
        by_seq = sorted((lines[index] for index in indexes), key=lambda line: line.seq)
        for index, line in zip(indexes, by_seq, strict=True):
            ordered[index] = line
    return ordered


async def read_agent_log_entries(
    query: str, *, start: datetime, end: datetime
) -> tuple[list[dict], LiveLogUnavailable | None]:
    """The newest ``LIVE_LOG_LINE_LIMIT`` entries of ``query``, or why the store gave none."""
    if not os.environ.get("LOKI_URL"):
        return [], LiveLogUnavailable.NOT_CONFIGURED
    client = LokiClient()
    try:
        entries = await asyncio.wait_for(
            client.query_range(
                query, start=start, end=end, limit=LIVE_LOG_LINE_LIMIT, direction="backward"
            ),
            timeout=LOKI_READ_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        unavailable = LiveLogUnavailable.TIMED_OUT
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("agent_live_log_query_failed", error_type=type(exc).__name__)
        unavailable = LiveLogUnavailable.QUERY_FAILED
    else:
        return entries[:LIVE_LOG_LINE_LIMIT], None
    finally:
        await client.close()
    return [], unavailable


async def read_story_live_log(
    db: AsyncSession, story_id: str, now: datetime | None = None
) -> LiveLogResponse | None:
    """The story's live log from its creation to ``now``; ``None`` for an unknown story."""
    story = await db.get(Story, story_id)
    if story is None:
        return None
    brief = await db.scalar(select(ProductBrief).where(ProductBrief.story_id == story_id))
    tasks = (
        await db.scalars(
            select(Task).where(Task.story_id == story_id, Task.planning_attempt_id.is_not(None))
        )
    ).all()
    coverages: Sequence[RequirementCoverage] = []
    if brief is not None:
        coverages = (
            await db.scalars(
                select(RequirementCoverage).where(RequirementCoverage.brief_id == brief.id)
            )
        ).all()
    runs = (
        await db.scalars(select(Run).where(Run.story_id == story_id, Run.type.in_(_RUN_SOURCE)))
    ).all()
    sources, omitted = story_attempt_sources(brief, tasks, coverages, runs)
    if not sources:
        return LiveLogResponse(lines=[], unavailable=None, omitted_attempts=0)
    entries, unavailable = await read_agent_log_entries(
        story_live_log_query(sources),
        start=assume_utc(story.created_at),
        end=now or datetime.now(UTC),
    )
    return LiveLogResponse(
        lines=live_log_lines(entries, sources),
        unavailable=unavailable,
        omitted_attempts=omitted,
    )


async def read_worker_live_log(
    db: AsyncSession, worker_id: str, now: datetime | None = None
) -> LiveLogResponse:
    """The worker's live log of the last ``WORKER_LIVE_LOG_LOOKBACK``, sourced by Run type."""
    query = worker_live_log_query(worker_id)
    end = now or datetime.now(UTC)
    entries, unavailable = await read_agent_log_entries(
        query, start=end - WORKER_LIVE_LOG_LOOKBACK, end=end
    )
    attempt_ids = {entry["attempt_id"] for entry in entries}
    sources = dict.fromkeys(attempt_ids, LiveLogSource.WORKER)
    if attempt_ids:
        runs = (
            await db.scalars(select(Run).where(Run.id.in_(attempt_ids), Run.type.in_(_RUN_SOURCE)))
        ).all()
        sources.update({run.id: _RUN_SOURCE[run.type] for run in runs})
    return LiveLogResponse(
        lines=live_log_lines(entries, sources), unavailable=unavailable, omitted_attempts=0
    )
