"""Behavior contracts for independently restartable scheduler services."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from shared.config_store import ConfigStore
from shared.queues import PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP

REPO_ROOT = Path(__file__).resolve().parents[4]


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
    builds = [
        services[name]["build"]
        for name in (
            "scheduler-pipeline",
            "scheduler-infrastructure",
            "scheduler-maintenance",
        )
    ]
    assert builds[0] == builds[1] == builds[2]
    for name in ("scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"):
        assert not (
            {"scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"}
            & set(services[name]["depends_on"])
        )


@pytest.mark.asyncio
async def test_unexpected_worker_exit_cancels_its_process_siblings():
    from src import runtime

    sibling_stopped = asyncio.Event()

    async def exits():
        return None

    async def sibling():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_stopped.set()

    with pytest.raises(RuntimeError, match="long-lived worker exited: exits"):
        await runtime.run_workers(
            [("exits", exits), ("sibling", sibling)], service_name="scheduler-maintenance"
        )

    assert sibling_stopped.is_set()


@pytest.mark.asyncio
async def test_readiness_marker_is_current_and_removed_on_stop(tmp_path, monkeypatch):
    from src import runtime

    marker = tmp_path / "scheduler-ready"
    worker_observed_marker = asyncio.Event()

    async def worker():
        assert marker.read_text() == "scheduler-pipeline"
        worker_observed_marker.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "READINESS_PATH", marker)
    service = asyncio.create_task(
        runtime.run_workers([("worker", worker)], service_name="scheduler-pipeline")
    )
    await asyncio.wait_for(worker_observed_marker.wait(), timeout=1)
    assert marker.read_text() == "scheduler-pipeline"

    service.cancel()
    await service

    assert not marker.exists()


@pytest.mark.asyncio
async def test_config_startup_removes_stale_readiness_even_when_validation_fails(
    tmp_path, monkeypatch
):
    from src import runtime

    marker = tmp_path / "scheduler-ready"
    marker.write_text("scheduler-maintenance")
    monkeypatch.setattr(runtime, "READINESS_PATH", marker)
    monkeypatch.setattr(
        runtime.startup,
        "init_config",
        MagicMock(side_effect=RuntimeError("Missing required system configs")),
    )

    with pytest.raises(RuntimeError, match="Missing required system configs"):
        await runtime.initialize_configs({"scheduler.dispatch_interval_seconds"}, service_name="x")

    assert not marker.exists()


@pytest.mark.asyncio
async def test_pipeline_starts_without_loki(monkeypatch):
    from src import pipeline

    monkeypatch.delenv("LOKI_URL", raising=False)
    initialize = AsyncMock()
    run_workers = AsyncMock()
    monkeypatch.setattr(pipeline.runtime, "initialize_configs", initialize)
    monkeypatch.setattr(pipeline.runtime, "run_workers", run_workers)

    await pipeline.main()

    initialize.assert_awaited_once_with(
        pipeline.PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline"
    )
    workers = run_workers.await_args.args[0]
    assert [(name, worker) for name, worker in workers] == [
        ("task_dispatcher", pipeline.task_dispatcher_loop)
    ]


@pytest.mark.asyncio
async def test_worker_inventory_is_complete_and_disjoint(monkeypatch):
    from src import infrastructure, maintenance, pipeline

    inventories = {}

    async def capture(module, service_name):
        run_workers = AsyncMock()
        monkeypatch.setattr(module.runtime, "initialize_configs", AsyncMock())
        monkeypatch.setattr(module.runtime, "run_workers", run_workers)
        if module is infrastructure:
            monkeypatch.setattr(module, "validate_provider_policies", lambda: None)
            monkeypatch.setattr(module, "managed_provider_ids", lambda _provider: frozenset())
            monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
            monkeypatch.setattr(module, "retry_pending_servers", AsyncMock())
        await module.main()
        assert run_workers.await_args.kwargs["service_name"] == service_name
        inventories[service_name] = {name for name, _worker in run_workers.await_args.args[0]}

    await capture(pipeline, "scheduler-pipeline")
    await capture(infrastructure, "scheduler-infrastructure")
    await capture(maintenance, "scheduler-maintenance")

    assert set().union(*inventories.values()) == {
        "task_dispatcher",
        "server_sync",
        "health_checker",
        "provisioner_results",
        "github_sync",
        "rag_summarizer",
        "analytics_aggregator",
        "queue_cleanup",
    }
    pipeline_workers, infrastructure_workers, maintenance_workers = inventories.values()
    assert not pipeline_workers & infrastructure_workers
    assert not pipeline_workers & maintenance_workers
    assert not infrastructure_workers & maintenance_workers


@pytest.mark.asyncio
async def test_provisioner_consumer_preserves_recovery_topology(monkeypatch):
    from src.tasks import provisioner_result_listener

    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()

    async def entries(*args, **kwargs):
        if False:
            yield None

    client.consume = MagicMock(side_effect=entries)
    monkeypatch.setattr(
        provisioner_result_listener, "RedisStreamClient", MagicMock(return_value=client)
    )

    await provisioner_result_listener.provisioner_results_worker()

    client.consume.assert_called_once_with(
        PROVISIONER_RESULTS,
        SCHEDULER_CONSUMER_GROUP,
        provisioner_result_listener.CONSUMER_NAME,
        auto_ack=False,
        claim_pending=True,
    )
    client.close.assert_awaited_once()
