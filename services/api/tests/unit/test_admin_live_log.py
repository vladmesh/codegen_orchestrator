"""The live agent log read of a story and of a worker, against a fake Loki and a fake session."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.clients.loki import LokiClient
from shared.models import ProductBrief, RequirementCoverage, Run, Story, Task
from src import admin_live_log
from src.admin_live_log import (
    read_story_live_log,
    read_worker_live_log,
    story_live_log_query,
    worker_live_log_query,
)
from src.database import get_async_session
from src.main import app
from src.schemas.admin_console import LiveLogUnavailable

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=2)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class FakeSession:
    """Answers each query with the rows given for its model, as if the database had filtered."""

    def __init__(self, story=None, brief=None, rows=None):
        self.story = story
        self.brief = brief
        self.rows = rows or {}

    async def get(self, model, key):
        assert model is Story
        return self.story

    async def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is ProductBrief
        return self.brief

    async def scalars(self, statement):
        rows = self.rows.get(statement.column_descriptions[0]["entity"], [])
        return SimpleNamespace(all=lambda: rows)


def _row(**fields):
    return SimpleNamespace(**fields)


def _run(run_id, type, minutes):
    return _row(id=run_id, type=type, created_at=at(minutes))


def _story_session(runs=(), *, brief_attempt="plan-b", task_attempt="plan-a"):
    return FakeSession(
        story=_row(id="st-1", created_at=at(0)),
        brief=_row(id="brief-1", planning_attempt_id=brief_attempt, updated_at=at(30)),
        rows={
            Task: [_row(planning_attempt_id=task_attempt, created_at=at(2))],
            RequirementCoverage: [_row(planning_attempt_id=brief_attempt, created_at=at(20))],
            Run: list(runs),
        },
    )


def _line(nanos, attempt_id, seq, kind="info", text="t", **labels):
    payload = {
        "event": "agent_log_line",
        "level": "info",
        "timestamp": "ignored",
        "attempt_id": attempt_id,
        "task_id": None,
        "seq": seq,
        "kind": kind,
        "text": text,
    }
    return labels, [str(nanos), json.dumps(payload)]


def _loki_entries(*lines):
    """Entries as the real client parses them out of a Loki response."""
    return LokiClient._parse_response(
        {
            "data": {
                "result": [{"stream": labels, "values": [value]} for labels, value in lines],
            }
        }
    )


def _fake_loki(entries=None, error=None):
    client = AsyncMock()
    if error is not None:
        client.query_range.side_effect = error
    else:
        client.query_range.return_value = entries or []
    return client


SECOND = 1_000_000_000
BASE_NS = int(at(10).timestamp()) * SECOND


def test_the_story_query_selects_only_agent_log_line_events_of_exactly_its_ids():
    query = story_live_log_query(["qa-1", "plan-a", "eng-1"])

    assert query == (
        '{job="docker"} |= "agent_log_line" | json | event="agent_log_line"'
        ' | attempt_id=~"eng-1|plan-a|qa-1"'
    )


def test_the_worker_query_selects_agent_log_line_events_of_its_label():
    assert worker_live_log_query("qa-1a2b3c") == (
        '{worker_id="qa-1a2b3c"} |= "agent_log_line" | json | event="agent_log_line"'
    )


@pytest.mark.parametrize("bad", ['eng-1"} |= "', "a|b", "x" * 65, "", "plan_a"])
def test_an_unsafe_id_is_never_interpolated(bad):
    with pytest.raises(ValueError, match="not a safe log search id"):
        story_live_log_query(["eng-1", bad])
    with pytest.raises(ValueError, match="not a safe log search id"):
        worker_live_log_query(bad)


async def test_a_story_with_an_unsafe_run_id_raises_before_asking_loki(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    client = _fake_loki()
    session = _story_session([_run('eng-1"}', "engineering", 5)])

    with (
        patch("src.admin_live_log.LokiClient", return_value=client),
        pytest.raises(ValueError, match="not a safe log search id"),
    ):
        await read_story_live_log(session, "st-1", now=NOW)

    client.query_range.assert_not_awaited()


async def test_story_lines_map_to_their_source_and_keep_seq_order(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    entries = _loki_entries(
        _line(BASE_NS + 5 * SECOND, "qa-1", 0, "step", compose_service=""),
        # One attempt's lines arrive with Loki times that disagree with their seq.
        _line(BASE_NS + 2 * SECOND, "eng-1", 1, "cmd"),
        _line(BASE_NS + 2 * SECOND, "eng-1", 0, "info"),
        _line(BASE_NS + 3 * SECOND, "eng-1", 3, "ok"),
        _line(BASE_NS + 3 * SECOND + 1500, "eng-1", 2, "file"),
        _line(BASE_NS, "plan-a", 0, "step", compose_service="langgraph"),
    )
    client = _fake_loki(entries)
    session = _story_session(
        [_run("eng-1", "engineering", 5), _run("qa-1", "qa", 40)],
    )

    with patch("src.admin_live_log.LokiClient", return_value=client):
        result = await read_story_live_log(session, "st-1", now=NOW)

    assert result.unavailable is None
    assert result.omitted_attempts == 0
    assert [(line.source.value, line.attempt_id, line.seq) for line in result.lines] == [
        ("architect", "plan-a", 0),
        ("developer", "eng-1", 0),
        ("developer", "eng-1", 1),
        ("developer", "eng-1", 2),
        ("developer", "eng-1", 3),
        ("qa", "qa-1", 0),
    ]
    assert result.lines[0].at == at(10)
    kwargs = client.query_range.await_args.kwargs
    assert client.query_range.await_args.args[0] == story_live_log_query(
        ["plan-a", "plan-b", "eng-1", "qa-1"]
    )
    assert (kwargs["start"], kwargs["end"]) == (at(0), NOW)
    assert kwargs["direction"] == "backward"
    assert kwargs["limit"] == admin_live_log.LIVE_LOG_LINE_LIMIT
    client.close.assert_awaited_once()


async def test_a_story_beyond_the_id_cap_queries_its_newest_ids_and_counts_the_rest(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    monkeypatch.setattr(admin_live_log, "LIVE_LOG_ATTEMPT_ID_CAP", 3)
    client = _fake_loki()
    session = _story_session(
        [_run("eng-1", "engineering", 5), _run("eng-2", "engineering", 25), _run("qa-1", "qa", 40)]
    )

    with patch("src.admin_live_log.LokiClient", return_value=client):
        result = await read_story_live_log(session, "st-1", now=NOW)

    # A planning attempt is as old as its first row: plan-b's coverage, not the brief's update.
    assert result.omitted_attempts == 2
    queried = client.query_range.await_args.args[0]
    assert queried == story_live_log_query(["plan-b", "eng-2", "qa-1"])


async def test_an_unknown_story_reads_nothing():
    assert await read_story_live_log(FakeSession(), "st-missing", now=NOW) is None


async def _never_answers(*args, **kwargs):
    await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("configured", "error", "timeout", "reason"),
    [
        (False, None, None, LiveLogUnavailable.NOT_CONFIGURED),
        (True, httpx.ConnectError("down"), None, LiveLogUnavailable.QUERY_FAILED),
        (True, _never_answers, 0, LiveLogUnavailable.TIMED_OUT),
    ],
)
async def test_an_unavailable_log_store_returns_its_reason_and_no_lines(
    monkeypatch, configured, error, timeout, reason
):
    if configured:
        monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    else:
        monkeypatch.delenv("LOKI_URL", raising=False)
    if timeout is not None:
        monkeypatch.setattr(admin_live_log, "LOKI_READ_TIMEOUT_SECONDS", timeout)
    client = _fake_loki(error=error)

    with patch("src.admin_live_log.LokiClient", return_value=client):
        story = await read_story_live_log(
            _story_session([_run("eng-1", "engineering", 5)]), "st-1", now=NOW
        )
        worker = await read_worker_live_log(FakeSession(), "w-1", now=NOW)

    for result in (story, worker):
        assert result.unavailable is reason
        assert result.lines == []


async def test_the_worker_read_labels_sources_from_run_types_and_keeps_unmatched_lines(
    monkeypatch,
):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    entries = _loki_entries(
        _line(BASE_NS, "eng-1", 0, worker_id="w-1"),
        _line(BASE_NS + SECOND, "qa-1", 0, worker_id="w-1"),
        _line(BASE_NS + 2 * SECOND, "attempt-without-run", 0, worker_id="w-1"),
    )
    client = _fake_loki(entries)
    session = FakeSession(rows={Run: [_run("eng-1", "engineering", 5), _run("qa-1", "qa", 6)]})

    with patch("src.admin_live_log.LokiClient", return_value=client):
        result = await read_worker_live_log(session, "w-1", now=NOW)

    assert result.unavailable is None
    assert [(line.attempt_id, line.source.value) for line in result.lines] == [
        ("eng-1", "developer"),
        ("qa-1", "qa"),
        ("attempt-without-run", "worker"),
    ]
    assert client.query_range.await_args.args[0] == worker_live_log_query("w-1")
    kwargs = client.query_range.await_args.kwargs
    assert kwargs["end"] - kwargs["start"] == admin_live_log.WORKER_LIVE_LOG_LOOKBACK


@pytest.fixture
def _session_override():
    def install(session):
        app.dependency_overrides[get_async_session] = lambda: session

    yield install
    app.dependency_overrides.pop(get_async_session, None)


async def test_the_live_log_routes_answer_with_utc_lines_and_404_an_unknown_story(
    monkeypatch, _session_override
):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    client = _fake_loki(_loki_entries(_line(BASE_NS, "eng-1", 0, "cmd", "ls")))
    _session_override(_story_session([_run("eng-1", "engineering", 5)]))

    with patch("src.admin_live_log.LokiClient", return_value=client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/api/admin/v2/journeys/st-1/live-log")).status_code == 401
            assert (await http.get("/api/admin/v2/workers/w-1/live-log")).status_code == 401
            found = await http.get("/api/admin/v2/journeys/st-1/live-log", headers=INTERNAL_HEADERS)
            _session_override(FakeSession())
            missing = await http.get(
                "/api/admin/v2/journeys/st-missing/live-log", headers=INTERNAL_HEADERS
            )

    assert found.status_code == 200, found.text
    assert found.json() == {
        "lines": [
            {
                "at": "2026-10-07T14:10:00Z",
                "source": "developer",
                "attempt_id": "eng-1",
                "seq": 0,
                "kind": "cmd",
                "text": "ls",
            }
        ],
        "unavailable": None,
        "omitted_attempts": 0,
    }
    assert missing.status_code == 404
