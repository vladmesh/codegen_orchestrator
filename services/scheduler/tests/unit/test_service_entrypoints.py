"""Behavior contracts for independently restartable scheduler services."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from shared.config_store import ConfigStore
from shared.queues import PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP

REPO_ROOT = Path(__file__).resolve().parents[4]


def _run_pipeline_process(started):
    from src import pipeline

    async def initialize(*_args, **_kwargs):
        return None

    async def dispatcher():
        started.set()
        await asyncio.Event().wait()

    pipeline.runtime.initialize_configs = initialize
    pipeline.task_dispatcher_loop = dispatcher
    asyncio.run(pipeline.main())


def _run_infrastructure_process(started):
    from src import infrastructure

    async def initialize(*_args, **_kwargs):
        return None

    async def no_wait(*_args, **_kwargs):
        return None

    async def run_workers(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    infrastructure.runtime.initialize_configs = initialize
    infrastructure.runtime.run_workers = run_workers
    infrastructure.asyncio.sleep = no_wait
    infrastructure.retry_pending_servers = no_wait
    infrastructure.validate_provider_policies = lambda: None
    infrastructure.managed_provider_ids = lambda _provider: frozenset()
    asyncio.run(infrastructure.main())


def _run_broken_maintenance_process():
    from src import maintenance

    os.environ.pop("LOKI_URL", None)
    asyncio.run(maintenance.main())


def test_config_ownership_matches_runtime_callers():
    from src import startup

    owned = (
        startup.PIPELINE_REQUIRED_KEYS
        | startup.INFRASTRUCTURE_REQUIRED_KEYS
        | startup.MAINTENANCE_REQUIRED_KEYS
    )

    assert owned == set(startup.REQUIRED_KEYS)
    assert not (startup.PIPELINE_REQUIRED_KEYS & startup.INFRASTRUCTURE_REQUIRED_KEYS)
    assert not (startup.PIPELINE_REQUIRED_KEYS & startup.MAINTENANCE_REQUIRED_KEYS)
    assert not (startup.INFRASTRUCTURE_REQUIRED_KEYS & startup.MAINTENANCE_REQUIRED_KEYS)


def test_pipeline_does_not_require_maintenance_or_infrastructure_config():
    from src import startup

    assert "scheduler.dispatch_interval_seconds" in startup.PIPELINE_REQUIRED_KEYS
    assert "scheduler.rag_summarizer_poll_interval" not in startup.PIPELINE_REQUIRED_KEYS
    assert "scheduler.server_sync_interval" not in startup.PIPELINE_REQUIRED_KEYS


def test_pipeline_validates_only_its_own_config(monkeypatch):
    from src import startup

    store = ConfigStore("http://api:8000")
    store._client = MagicMock()

    def config_response(path, **_kwargs):
        key = path.removeprefix("system-configs/")
        if key in startup.PIPELINE_REQUIRED_KEYS:
            response = MagicMock(status_code=200)
            response.json.return_value = {"key": key, "value": 1}
            return response
        return MagicMock(status_code=404)

    store._client.get_raw.side_effect = config_response
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setattr(startup, "ConfigStore", MagicMock(return_value=store))

    startup.init_config(startup.PIPELINE_REQUIRED_KEYS)

    with pytest.raises(RuntimeError, match="scheduler.rag_summarizer_poll_interval"):
        startup.init_config(startup.MAINTENANCE_REQUIRED_KEYS)


def test_compose_runs_independent_scheduler_processes():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    assert "scheduler" not in services
    assert services["scheduler-pipeline"]["command"] == ["python", "-m", "src.pipeline"]
    assert services["scheduler-infrastructure"]["command"] == [
        "python",
        "-m",
        "src.infrastructure",
    ]
    assert services["scheduler-maintenance"]["command"] == [
        "python",
        "-m",
        "src.maintenance",
    ]
    assert "loki" not in services["scheduler-pipeline"]["depends_on"]
    assert "LOKI_URL" not in services["scheduler-pipeline"]["environment"]
    assert services["scheduler-maintenance"]["environment"]["LOKI_URL"] == "${LOKI_URL:-}"
    for name in ("scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"):
        assert not (
            {"scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"}
            & set(services[name]["depends_on"])
        )


@pytest.mark.asyncio
async def test_unexpected_worker_exit_cancels_its_process_siblings():
    from src.runtime import run_workers

    sibling_stopped = asyncio.Event()

    async def exits():
        return None

    async def sibling():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_stopped.set()

    with pytest.raises(RuntimeError, match="long-lived worker exited: exits"):
        await run_workers(
            [("exits", exits), ("sibling", sibling)], service_name="scheduler-maintenance"
        )

    assert sibling_stopped.is_set()


@pytest.mark.asyncio
async def test_pipeline_starts_without_loki(monkeypatch):
    from src import pipeline

    monkeypatch.delenv("LOKI_URL", raising=False)
    initialize = AsyncMock()
    dispatcher = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(pipeline.runtime, "initialize_configs", initialize)
    monkeypatch.setattr(pipeline, "task_dispatcher_loop", dispatcher)

    await pipeline.main()

    initialize.assert_awaited_once_with(
        pipeline.PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline"
    )
    dispatcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_maintenance_fails_before_config_or_workers_when_loki_is_missing(monkeypatch):
    from src import maintenance

    monkeypatch.delenv("LOKI_URL", raising=False)
    initialize = AsyncMock()
    run_workers = AsyncMock()
    monkeypatch.setattr(maintenance.runtime, "initialize_configs", initialize)
    monkeypatch.setattr(maintenance.runtime, "run_workers", run_workers)

    with pytest.raises(RuntimeError, match="LOKI_URL is not set"):
        await maintenance.main()

    initialize.assert_not_awaited()
    run_workers.assert_not_awaited()


def test_maintenance_process_failure_leaves_pipeline_and_infrastructure_running():
    """A process-local startup failure cannot cancel either critical service."""
    context = multiprocessing.get_context("fork")
    pipeline_started = context.Event()
    infrastructure_started = context.Event()
    pipeline_process = context.Process(target=_run_pipeline_process, args=(pipeline_started,))
    infrastructure_process = context.Process(
        target=_run_infrastructure_process, args=(infrastructure_started,)
    )
    maintenance_process = context.Process(target=_run_broken_maintenance_process)

    pipeline_process.start()
    infrastructure_process.start()
    try:
        assert pipeline_started.wait(timeout=5)
        assert infrastructure_started.wait(timeout=5)
        maintenance_process.start()
        maintenance_process.join(timeout=5)

        assert maintenance_process.exitcode not in (None, 0)
        assert pipeline_process.is_alive()
        assert infrastructure_process.is_alive()
    finally:
        for process in (pipeline_process, infrastructure_process, maintenance_process):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)


@pytest.mark.asyncio
async def test_provisioner_consumer_preserves_recovery_topology(monkeypatch):
    from src import infrastructure

    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()

    async def entries(*args, **kwargs):
        if False:
            yield None

    client.consume = MagicMock(side_effect=entries)
    monkeypatch.setattr(infrastructure, "RedisStreamClient", MagicMock(return_value=client))

    await infrastructure.provisioner_results_worker()

    client.consume.assert_called_once_with(
        PROVISIONER_RESULTS,
        SCHEDULER_CONSUMER_GROUP,
        infrastructure.CONSUMER_NAME,
        auto_ack=False,
        claim_pending=True,
    )
    client.close.assert_awaited_once()
