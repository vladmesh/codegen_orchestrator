"""Behavior contracts for independently restartable scheduler services."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

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
    assert "scheduler.github_sync_missing_threshold" not in startup.PIPELINE_REQUIRED_KEYS
    assert "scheduler.server_sync_interval" not in startup.PIPELINE_REQUIRED_KEYS


async def test_pipeline_validates_only_its_own_config(monkeypatch):
    from src import startup

    def config_response(_request: httpx.Request) -> httpx.Response:
        body = [{"key": key, "value": 1} for key in startup.PIPELINE_REQUIRED_KEYS]
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "shared.clients.internal_api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(config_response), **kwargs),
    )
    monkeypatch.setenv("API_BASE_URL", "http://api:8000")
    monkeypatch.setattr(startup, "config", None)

    await startup.init_config(startup.PIPELINE_REQUIRED_KEYS)

    with pytest.raises(RuntimeError, match="scheduler.github_sync_missing_threshold"):
        await startup.init_config(startup.MAINTENANCE_REQUIRED_KEYS)


class _Refresher:
    """Stands in for the process ConfigStore: records that its refresher ran and stopped."""

    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def refresh_forever(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@pytest.fixture
def config_refresher(monkeypatch) -> _Refresher:
    from src import startup

    refresher = _Refresher()
    monkeypatch.setattr(startup, "config", refresher)
    return refresher


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
    expected_healthcheck = {
        "test": [
            "CMD-SHELL",
            '[ "$$(cat /tmp/scheduler-service-ready 2>/dev/null)" = "$$SERVICE_NAME" ]',
        ],
        "interval": "5s",
        "timeout": "3s",
        "retries": 3,
        "start_period": "120s",
    }
    for name in ("scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"):
        assert services[name]["healthcheck"] == expected_healthcheck
        assert not (
            {"scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance"}
            & set(services[name]["depends_on"])
        )


def test_scheduler_pipeline_carries_the_registry_env_its_merges_write():
    """The merging process writes the product's registry secrets, so it holds them.

    `pr_poller` and `story_completion` run in scheduler-pipeline and refresh a
    product repository's REGISTRY_* secrets before every merge they perform or
    enable. Production composes this file, so the variables must be named here.
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    environment = compose["services"]["scheduler-pipeline"]["environment"]

    for variable in ("ORCHESTRATOR_HOSTNAME", "REGISTRY_USER", "REGISTRY_PASSWORD"):
        assert environment[variable] == f"${{{variable}}}"


def test_infra_stack_waits_for_scheduler_health():
    compose = yaml.safe_load((REPO_ROOT / "tests/compose/integration/infra.yml").read_text())
    services = compose["services"]

    assert services["scheduler-pipeline"]["healthcheck"]
    assert services["scheduler-infrastructure"]["healthcheck"]
    assert "SERVICE_NAME=scheduler-pipeline" in services["scheduler-pipeline"]["environment"]
    assert (
        "SERVICE_NAME=scheduler-infrastructure"
        in services["scheduler-infrastructure"]["environment"]
    )
    runner_dependencies = services["integration-test-runner"]["depends_on"]
    assert runner_dependencies["scheduler-pipeline"]["condition"] == "service_healthy"
    assert runner_dependencies["scheduler-infrastructure"]["condition"] == "service_healthy"


@pytest.mark.asyncio
async def test_unexpected_worker_exit_cancels_its_process_siblings(config_refresher):
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
    assert config_refresher.stopped.is_set()


@pytest.mark.asyncio
async def test_one_config_refresher_runs_beside_the_workers(
    config_refresher, tmp_path, monkeypatch
):
    from src import runtime

    refreshing_while_working = asyncio.Event()

    async def worker():
        await config_refresher.started.wait()
        refreshing_while_working.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "READINESS_PATH", tmp_path / "scheduler-ready")
    service = asyncio.create_task(
        runtime.run_workers([("worker", worker)], service_name="scheduler-pipeline")
    )
    await asyncio.wait_for(refreshing_while_working.wait(), timeout=1)

    service.cancel()
    await service

    assert config_refresher.stopped.is_set()


@pytest.mark.asyncio
async def test_periodic_loop_cycles_do_not_share_a_correlation_id():
    import structlog

    from shared.contracts.queues.architect import ArchitectMessage
    from shared.log_config import ensure_correlation_id, set_correlation_id
    from src import runtime

    class Stop(Exception):
        pass

    published: list[str] = []

    async def cycle(_redis):
        ensure_correlation_id()
        message = ArchitectMessage(story_id="story-1", project_id="proj-1")
        published.append(message.correlation_id)

    async def sleep(_seconds):
        if len(published) == 2:
            raise Stop

    redis_client = MagicMock(connect=AsyncMock(), close=AsyncMock())
    structlog.contextvars.clear_contextvars()
    set_correlation_id("startup")
    try:
        with pytest.raises(Stop):
            await runtime.periodic_loop(
                interval=lambda: 0,
                cycle=cycle,
                logger=MagicMock(),
                started_event="started",
                cycle_event=None,
                error_event="failed",
                stopped_event="stopped",
                redis_factory=lambda: redis_client,
                sleep=sleep,
            )
    finally:
        structlog.contextvars.clear_contextvars()

    assert "startup" not in published
    assert len(set(published)) == 2


@pytest.mark.asyncio
async def test_readiness_marker_is_current_and_removed_on_stop(
    tmp_path, monkeypatch, config_refresher
):
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
    workers = dict(run_workers.await_args.args[0])
    assert list(workers) == [
        "task_dispatcher",
        "scaffold",
        "story_completion",
        "scheduler_wake",
        "lifecycle_supervision",
        "qa_routing",
        "pr_ci",
        "worker_reconciliation",
        "temporary_access",
        "owner_notifications",
        "story_supervision",
    ]
    assert workers["scaffold"] is pipeline.scaffold_loop
    assert workers["worker_reconciliation"] is pipeline.worker_reconciliation_loop
    assert workers["owner_notifications"] is pipeline.owner_notification_loop
    assert workers["story_supervision"] is pipeline.story_supervision_loop


@pytest.mark.asyncio
async def test_pipeline_routes_each_hint_only_to_its_own_loop(monkeypatch):
    from _wake_doubles import Broker, Timers, settle

    from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
    from shared.redis.scheduler_wake import publish_scheduler_wake
    from src import pipeline
    from src.tasks import (
        lifecycle_supervision_loop,
        pr_ci_loop,
        qa_routing_loop,
        scheduler_wake_listener,
        story_completion_loop,
        task_dispatcher,
        temporary_access_loop,
    )

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    broker = Broker()
    journal: list[str] = []
    lifecycle_sweeps = [
        "supervise_stuck_stories",
        "supervise_stuck_tasks",
        "supervise_failed_tasks",
        "supervise_waiting_resource_tasks",
        "supervise_application_deploy_handoffs",
        "supervise_deploying_stories",
        "supervise_waiting_user_secret_stories",
    ]

    def record(step: str, result: object = None) -> AsyncMock:
        return AsyncMock(side_effect=lambda *_args: journal.append(step) or result)

    run_workers = AsyncMock()
    monkeypatch.setattr(pipeline.runtime, "initialize_configs", AsyncMock())
    monkeypatch.setattr(pipeline.runtime, "run_workers", run_workers)
    monkeypatch.setattr(story_completion_loop, "RedisStreamClient", broker.client)
    monkeypatch.setattr(story_completion_loop, "complete_stories", record("complete", 0))
    monkeypatch.setattr(story_completion_loop, "_story_completion_interval", lambda: 3600)
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", broker.client)
    monkeypatch.setattr(task_dispatcher, "dispatch_todo_tasks", record("dispatch", 0))
    monkeypatch.setattr(task_dispatcher, "_recover_catalog_installs", record("recover"))
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: 3600)
    monkeypatch.setattr(lifecycle_supervision_loop, "RedisStreamClient", broker.client)
    for sweep in lifecycle_sweeps:
        monkeypatch.setattr(lifecycle_supervision_loop, sweep, record(sweep, {}))
    monkeypatch.setattr(lifecycle_supervision_loop, "_lifecycle_supervision_interval", lambda: 3600)
    monkeypatch.setattr(temporary_access_loop, "RedisStreamClient", broker.client)
    monkeypatch.setattr(
        temporary_access_loop, "supervise_temporary_access", record("temporary_access", {})
    )
    monkeypatch.setattr(temporary_access_loop, "_temporary_access_interval", lambda: 3600)
    monkeypatch.setattr(qa_routing_loop, "RedisStreamClient", broker.client)
    monkeypatch.setattr(qa_routing_loop, "supervise_testing_stories", record("qa_routing", {}))
    monkeypatch.setattr(qa_routing_loop, "_qa_routing_interval", lambda: 3600)
    monkeypatch.setattr(pr_ci_loop, "RedisStreamClient", broker.client)
    monkeypatch.setattr(pr_ci_loop, "poll_merged_prs", record("merged_prs", 0))
    monkeypatch.setattr(pr_ci_loop, "poll_ci_failures", record("ci_failures", 0))
    monkeypatch.setattr(pr_ci_loop, "_pr_ci_interval", lambda: 3600)
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: 3600)
    await pipeline.main()
    workers = dict(run_workers.await_args.args[0])

    tasks = [
        asyncio.create_task(workers["task_dispatcher"]()),
        asyncio.create_task(workers["story_completion"]()),
        asyncio.create_task(workers["lifecycle_supervision"]()),
        asyncio.create_task(workers["temporary_access"]()),
        asyncio.create_task(workers["qa_routing"]()),
        asyncio.create_task(workers["pr_ci"]()),
        asyncio.create_task(
            workers["scheduler_wake"](redis_factory=broker.client, sleep=Timers().sleep)
        ),
    ]
    await broker.subscribed()
    await settle()
    assert sorted(journal) == sorted(
        [
            "complete",
            "dispatch",
            "recover",
            "temporary_access",
            "qa_routing",
            "merged_prs",
            "ci_failures",
            *lifecycle_sweeps,
        ]
    )
    publisher = broker.client()
    await publisher.connect()

    hinted_steps = {
        SchedulerWakeTarget.TASK_DISPATCHER: ["dispatch", "recover"],
        SchedulerWakeTarget.STORY_COMPLETION: ["complete"],
        SchedulerWakeTarget.LIFECYCLE_SUPERVISION: lifecycle_sweeps,
        SchedulerWakeTarget.TEMPORARY_ACCESS: ["temporary_access"],
        SchedulerWakeTarget.QA_ROUTING: ["qa_routing"],
        SchedulerWakeTarget.PR_CI: ["merged_prs", "ci_failures"],
    }
    assert set(hinted_steps) == set(SchedulerWakeTarget)
    for target, steps in hinted_steps.items():
        journal.clear()
        await publish_scheduler_wake(publisher, target)
        await settle()
        assert journal == steps, target

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
        "scaffold",
        "story_completion",
        "lifecycle_supervision",
        "qa_routing",
        "pr_ci",
        "worker_reconciliation",
        "temporary_access",
        "owner_notifications",
        "story_supervision",
        "scheduler_wake",
        "server_sync",
        "health_checker",
        "provisioner_results",
        "github_sync",
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
