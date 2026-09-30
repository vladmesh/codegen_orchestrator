"""The story log read: a fixed, injection-proof query and only named, redacted fields."""

from unittest.mock import AsyncMock, patch

import pytest

from src.story_diagnostics_logs import read_story_logs, story_log_query, to_log_line

STORY = "story-3990e41c"
PROJECT = "b8c4f38b-6f0c-4c50-8932-24aa34ca9b56"
TOKEN = "ghs_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4"  # noqa: S105 - a fake installation token


def test_the_query_selects_error_and_warning_lines_naming_the_story_or_project():
    query = story_log_query(STORY, PROJECT)

    assert query.startswith('{compose_service=~".+"}')
    assert f'|~ "{STORY}|{PROJECT}"' in query
    assert 'level=~"error|warning"' in query


@pytest.mark.parametrize("bad", ['story"} |= "', "a|b", "x" * 65, ""])
def test_an_id_that_could_change_the_query_is_refused(bad):
    with pytest.raises(ValueError, match="not a safe log search id"):
        story_log_query(bad, PROJECT)


def test_a_line_keeps_only_named_fields_redacted_and_bounded():
    entry = {
        "timestamp": "2026-09-24T14:38:04.312826",
        "service": "scaffolder",
        "level": "error",
        "event": "scaffold_job_failed",
        "error": f"clone https://x-access-token:{TOKEN}@github.com/o/p failed " + "y" * 900,
        "github_token": TOKEN,
        "_labels": {"compose_service": "scaffolder"},
    }

    line = to_log_line(entry)

    dumped = line.model_dump()
    assert set(dumped) == {"timestamp", "service", "level", "event", "error"}
    assert TOKEN not in str(dumped)
    assert len(line.error) <= 303
    assert line.event == "scaffold_job_failed"


def test_the_service_falls_back_to_the_compose_label():
    line = to_log_line({"level": "warning", "_labels": {"compose_service": "architect"}})

    assert line.service == "architect"


@pytest.mark.asyncio
async def test_without_a_log_store_the_read_says_so(monkeypatch):
    monkeypatch.delenv("LOKI_URL", raising=False)

    logs, unavailable = await read_story_logs(STORY, PROJECT, hours=6, limit=20)

    assert logs == []
    assert "LOKI_URL" in unavailable


@pytest.mark.asyncio
async def test_a_failing_log_store_never_fails_the_read(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    client = AsyncMock()
    client.query_range.side_effect = ConnectionError("down")

    with patch("src.story_diagnostics_logs.LokiClient", return_value=client):
        logs, unavailable = await read_story_logs(STORY, PROJECT, hours=6, limit=20)

    assert logs == []
    assert unavailable == "log store could not be read (ConnectionError)"
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_read_asks_for_the_newest_lines_and_caps_them(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")
    client = AsyncMock()
    client.query_range.return_value = [{"level": "error", "event": f"e{i}"} for i in range(30)]

    with patch("src.story_diagnostics_logs.LokiClient", return_value=client):
        logs, unavailable = await read_story_logs(STORY, PROJECT, hours=6, limit=20)

    assert unavailable is None
    assert len(logs) == 20
    kwargs = client.query_range.await_args.kwargs
    assert kwargs["direction"] == "backward"
    assert kwargs["limit"] == 20
