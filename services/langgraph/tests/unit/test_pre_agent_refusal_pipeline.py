"""Cross-service proof for pre-agent refusal evidence and free parking."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringExecutionPhase,
    EngineeringInfrastructureRefusal,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskDTO
from shared.contracts.vocab import AgentType
from src.clients.worker_spawner import _wait_until_ready
from src.nodes.developer import DeveloperNode
from tests.unit.factories import make_project, make_repository


def _load_service_package(name: str, source: Path):
    """Load another service's `src` package without replacing LangGraph's `src`."""
    spec = importlib.util.spec_from_file_location(
        name,
        source / "__init__.py",
        submodule_search_locations=[str(source)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


@pytest.mark.asyncio
async def test_worker_manager_refusal_reaches_one_tick_supervisor_park(monkeypatch):
    """The production evidence shape parks at the retry bound without spending a retry."""
    root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("WORKER_BROKER_INTERNAL_TOKEN", "test-broker-token")
    _load_service_package("worker_manager_src", root / "services/worker-manager/src")
    _load_service_package("scheduler_src", root / "services/scheduler/src")

    from scheduler_src.tasks.supervisor import liveness  # noqa: PLC0415
    from worker_manager_src.manager import (  # noqa: PLC0415
        EngineeringWorkerCreationRefusal,
        WorkerManager,
    )

    from src.consumers import engineering  # noqa: PLC0415

    manager_redis = AsyncMock()
    manager = SimpleNamespace(redis=manager_redis)
    refusal = EngineeringWorkerCreationRefusal(
        EngineeringInfrastructureRefusal.PROJECT_LOCKED,
        "project checkout is held",
    )
    await WorkerManager._reject_worker(manager, "worker-1", refusal)
    status_fields = manager_redis.hset.await_args.kwargs["mapping"]

    spawner_redis = AsyncMock()
    spawner_redis.hgetall.return_value = status_fields
    spawner_redis.get.return_value = "project checkout is held"
    spawn_result = await _wait_until_ready(spawner_redis, "worker-1", "request-1", timeout=1)
    assert spawn_result is not None
    assert spawn_result.execution is not None
    assert spawn_result.execution.execution_phase is EngineeringExecutionPhase.PRE_AGENT_REFUSED

    node_result = DeveloperNode._build_result_state(
        spawn_result,
        "test-project",
        "org/test-project",
        {
            "executor_decision": SimpleNamespace(agent_type=AgentType.CLAUDE),
            "project_spec": {"config": {}},
            "errors": [],
        },
    )
    assert node_result["execution"] == spawn_result.execution

    producer_api = AsyncMock()
    producer_api.get_project.return_value = make_project(name="test-project", status="active")
    producer_api.get_primary_repository.return_value = make_repository(
        git_url="https://github.com/org/test-project"
    )
    producer_api.get_run.return_value = SimpleNamespace(run_metadata={})
    producer_redis = AsyncMock()
    producer_redis.redis = AsyncMock()
    producer_redis.redis.hget.return_value = None

    with (
        patch.object(engineering, "api_client", producer_api),
        patch("src.consumers.engineering_result_handler.api_client", producer_api),
        patch.object(
            engineering.resource_allocator_node,
            "run",
            new_callable=AsyncMock,
            return_value={"allocated_resources": {}, "errors": []},
        ),
        patch("src.subgraphs.engineering.create_engineering_subgraph") as graph_factory,
        patch.object(engineering, "_build_story_context", new_callable=AsyncMock, return_value=""),
        patch.object(engineering, "_build_story_md", new_callable=AsyncMock, return_value=""),
        patch.object(engineering, "publish_callback_event", new_callable=AsyncMock),
        patch(
            "src.consumers.engineering_result_handler.prepare_terminal_settlement",
            new_callable=AsyncMock,
        ),
    ):
        graph_factory.return_value.ainvoke = AsyncMock(return_value=node_result)
        await engineering.process_engineering_job(
            {
                "task_id": "eng-1",
                "story_id": "story-1",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "initiating_run_id": "live-1",
                "planning_task_id": "task-1",
                "action": "feature",
                "description": "Exercise the refusal seam",
                "callback_stream": "po:input",
            },
            producer_redis,
        )

    terminal_patch = next(
        call.kwargs["json"]
        for call in producer_api.patch.await_args_list
        if call.kwargs["json"].get("status") == "failed"
    )
    assert terminal_patch["result"]["execution"] == terminal_patch["run_metadata"]["execution"]

    task = TaskDTO(
        id="task-1",
        project_id=UUID("00000000-0000-0000-0000-000000000001"),
        story_id="story-1",
        type="feature",
        title="Test task",
        status="failed",
        priority=0,
        current_iteration=3,
        max_iterations=3,
        created_by="system",
        dispatch_admitted=True,
        created_at=datetime.now(UTC),
    )
    scheduler_api = AsyncMock()
    scheduler_api.get_tasks_by_status.return_value = [task]
    scheduler_api.list_runs.return_value = [
        SimpleNamespace(id="eng-1", result=terminal_patch["result"])
    ]
    scheduler_api.get_story.return_value = SimpleNamespace(
        status=StoryStatus.IN_PROGRESS,
        quarantine_reason=None,
    )
    scheduler_redis = AsyncMock()

    with (
        patch.object(
            liveness,
            "owe_owner_notification",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(),
        ),
        patch.object(liveness, "deliver_owed_notification", new_callable=AsyncMock),
        patch.object(liveness, "_notify_admin_failure", new_callable=AsyncMock),
    ):
        result = await liveness.supervise_failed_tasks(scheduler_api, scheduler_redis)

    assert result == {"retried": 0, "escalated": 1}
    assert task.current_iteration == 3
    evidence = terminal_patch["result"]["execution"]
    scheduler_api.update_task.assert_awaited_once()
    assert (
        scheduler_api.update_task.await_args.args[1]["failure_metadata"][
            ENGINEERING_INFRASTRUCTURE_KEY
        ]["refusal"]
        == evidence["infrastructure_refusal"]
    )
    scheduler_api.transition_task.assert_awaited_once()
    scheduler_api.transition_story.assert_awaited_once()
