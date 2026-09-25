"""Tests for telegram_send_location in build_qa_callables (tools.py).

These tests run under the langgraph suite where `src.*` imports resolve.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.telegram_bot_probe import TELEGRAM_PROBE_PROCESS_TIMEOUT
from src.agents.qa.tools import build_qa_callables
from src.consumers._qa_target import QACapabilities
from src.consumers._qa_workspace import QAWorkspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WITH_BOT = QACapabilities(
    deployed_url="https://weather.example.com",
    physical_root="/opt/services/weather",
    containers=frozenset({"weather-backend"}),
    loopback_ports=frozenset({8000}),
    bot_username="weather_bot",
)

_NO_BOT = QACapabilities(
    deployed_url="https://weather.example.com",
    physical_root="/opt/services/weather",
    containers=frozenset({"weather-backend"}),
    loopback_ports=frozenset({8000}),
)


def _location_result(*, delivered: bool = True, text: str = "Погода") -> str:
    import json

    return (
        "telegram_probe_result:"
        + json.dumps(
            {
                "action": "location",
                "attempted": "send location (55.7558, 37.6176) to @weather_bot",
                "sent": "latitude=55.7558 longitude=37.6176",
                "delivered": delivered,
                "replies": (
                    [
                        {
                            "id": 20,
                            "text": text,
                            "caption": None,
                            "media_type": None,
                            "reply_markup": None,
                        }
                    ]
                    if delivered
                    else []
                ),
                "callback": None,
                "error": None if delivered else "ConnectionError: failed",
            }
        )
        + "\n"
    )


def _build(tmp_path, *, probe):
    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    return build_qa_callables(
        session=SimpleNamespace(capabilities=_WITH_BOT),
        workspace=workspace,
        telethon_env={"TELETHON_SESSION": "s"},
        probe_runner=probe,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_send_location_is_registered_with_bot(tmp_path):
    async def probe(script, *, env, timeout):
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    calls = _build(tmp_path, probe=probe)
    assert "telegram_send_location" in calls


@pytest.mark.asyncio
async def test_telegram_send_location_absent_without_bot(tmp_path):
    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    calls = build_qa_callables(
        session=SimpleNamespace(capabilities=_NO_BOT),
        workspace=workspace,
    )
    assert "telegram_send_location" not in calls


@pytest.mark.asyncio
async def test_telegram_send_location_passes_coordinates_in_script(tmp_path):
    scripts: list[str] = []

    async def probe(script, *, env, timeout):
        scripts.append(script)
        return SimpleNamespace(
            exit_status=0,
            stdout=_location_result(text="Получил геолокацию и проверяю текущую погоду."),
            stderr="",
        )

    calls = _build(tmp_path, probe=probe)
    answer = await calls["telegram_send_location"](55.7558, 37.6176)

    assert len(scripts) == 1
    assert "55.7558" in scripts[0]
    assert "37.6176" in scripts[0]
    assert "InputMediaGeoPoint" in scripts[0]
    assert answer["delivered"] is True
    assert answer["replies"][0]["text"] == "Получил геолокацию и проверяю текущую погоду."
    assert answer["action"] == "location"


@pytest.mark.asyncio
async def test_telegram_send_location_uses_standard_probe_timeout(tmp_path):
    timeouts: list[int] = []

    async def probe(script, *, env, timeout):
        timeouts.append(timeout)
        return SimpleNamespace(
            exit_status=0,
            stdout=_location_result(),
            stderr="",
        )

    calls = _build(tmp_path, probe=probe)
    await calls["telegram_send_location"](0.0, 0.0)

    assert timeouts == [TELEGRAM_PROBE_PROCESS_TIMEOUT]


@pytest.mark.asyncio
async def test_telegram_send_location_undelivered_blocks_verdict(tmp_path):
    """A delivery failure on send_location should set a blocker, not pass silently."""
    from shared.contracts.dto.run_result import QABlockerCategory

    async def probe(script, *, env, timeout):
        return SimpleNamespace(
            exit_status=0,
            stdout=_location_result(delivered=False),
            stderr="",
        )

    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    calls = build_qa_callables(
        session=SimpleNamespace(capabilities=_WITH_BOT),
        workspace=workspace,
        telethon_env={"TELETHON_SESSION": "s"},
        probe_runner=probe,
    )

    answer = await calls["telegram_send_location"](55.7558, 37.6176)

    assert "error" in answer
    assert workspace.telegram_probe_blocker is not None
    assert workspace.telegram_probe_blocker.category is QABlockerCategory.TELEGRAM_PROBE_UNDELIVERED
