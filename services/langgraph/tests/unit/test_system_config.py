"""The process system config: one awaited batch read at startup, reads without I/O."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.config_store import ConfigStoreUnavailableError
from src.config import system_config as process_config
from src.consumers import deploy, po
from src.llm.alerts import ALERT_CONFIG_CACHE_TTL_SECONDS, LLMAlerts

CONFIGS = {
    "deploy.deploy_lock_ttl": 120,
    "llm.summarization_max_tokens": 50_000,
    "llm.summarization_trigger_tokens": 60_000,
    "llm.summarization_max_summary_tokens": 2_000,
    "llm.alert_realert_window_hours": 2,
}


class _Responder:
    def __init__(self):
        self.paths: list[str] = []
        self.status_code = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        body = [{"key": key, "value": value} for key, value in CONFIGS.items()]
        return httpx.Response(self.status_code, json=body)


@pytest.fixture
def responder(monkeypatch) -> _Responder:
    rec = _Responder()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "shared.clients.internal_api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(rec), **kwargs),
    )
    return rec


@pytest.fixture
async def fresh_process(monkeypatch):
    """No store before the test; the refresher it starts is stopped after it."""
    monkeypatch.setattr(process_config, "_store", None)
    monkeypatch.setattr(process_config, "_refresher", None)
    monkeypatch.setattr(deploy, "system_config", process_config.system_config)
    yield
    refresher = process_config._refresher
    if refresher is not None:
        refresher.cancel()
        await asyncio.gather(refresher, return_exceptions=True)


async def _start(cache_ttl: float = 30):
    return await process_config.start_system_config("http://api:8000", cache_ttl=cache_ttl)


async def test_reads_before_start_fail(fresh_process):
    with pytest.raises(RuntimeError, match="not started"):
        deploy._deploy_lock_ttl()


async def test_deploy_lock_ttl_reads_the_started_snapshot_without_io(fresh_process, responder):
    await _start()

    assert [deploy._deploy_lock_ttl() for _ in range(5)] == [120] * 5
    assert responder.paths == ["/api/system-configs/"]


async def test_a_failed_first_refresh_fails_reads_closed(fresh_process, responder):
    responder.status_code = 503

    await _start()

    with pytest.raises(ConfigStoreUnavailableError, match="503"):
        deploy._deploy_lock_ttl()


async def test_po_tuning_and_alert_numbers_read_one_snapshot(fresh_process, responder):
    await _start(ALERT_CONFIG_CACHE_TTL_SECONDS)
    alerts = LLMAlerts.from_settings(SimpleNamespace(redis_url="redis://unused"))

    assert po.load_summarization_config() == po.SummarizationConfig(
        max_tokens=50_000, trigger_tokens=60_000, max_summary_tokens=2_000
    )
    assert await alerts.config_number("llm.alert_realert_window_hours", 6.0) == 2
    assert await alerts.config_number("llm.absent", 6.0) == 6.0
    assert responder.paths == ["/api/system-configs/"]


async def test_a_consumer_process_starts_its_system_config_before_consuming(
    fresh_process, responder
):
    from src.consumers._base import run_queue_worker

    async def consume(*_args, **_kwargs):
        assert deploy._deploy_lock_ttl() == 120
        for _ in ():
            yield

    redis = MagicMock(connect=AsyncMock(), close=AsyncMock(), consume=consume)
    with (
        patch("src.consumers._base.RedisStreamClient", return_value=redis),
        patch("src.consumers._base.api_client", MagicMock(close=AsyncMock())),
    ):
        await asyncio.wait_for(
            run_queue_worker("deploy-worker", "queue", AsyncMock(), system_config_ttl=30),
            timeout=2,
        )

    assert responder.paths == ["/api/system-configs/"]
