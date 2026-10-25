"""A planning Task's stored package build reaches the worker's TASK.md, or the attempt stops.

The consumer runs with the real developer node inside the subgraph; only the API,
GitHub and the worker transport are doubled. Rendered instructions prove what the
worker is told, never that a package exists or is compatible.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pydantic
import pytest

from shared.contracts.dto.local_install import LocalPackageBuild
from shared.contracts.dto.task import TaskDTO
from shared.contracts.queues.engineering import EngineeringMessage
from src.clients.worker_spawner import SpawnResult
from tests.unit.factories import make_engineering_reads, make_project, make_repository

PROJECT_ID = str(make_project().id)
SECTION = "## Product-Local Package Source"
BUILD = LocalPackageBuild(
    path="packages/post-ranking",
    package={
        "name": "post-ranking",
        "distribution": "codegen-kit-post-ranking",
        "version": "0.1.0",
    },
    core_version="2.6.0",
    python_version="3.12.0",
    tooling_commit="b" * 40,
)


def _job(**overrides) -> dict:
    return (
        EngineeringMessage(
            task_id="eng-1",
            project_id=PROJECT_ID,
            initiating_run_id="live-1",
            action="feature",
            description="Rank collected posts",
            skip_deploy=True,
            planning_task_id="task-build",
            story_id="story-1",
            branch="story/story-1",
        )
        .model_copy(update=overrides)
        .model_dump(mode="json")
    )


def _not_found(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"http://api/{path}")
    return httpx.HTTPStatusError(
        "not found", request=request, response=httpx.Response(404, request=request)
    )


async def _run(
    job: dict,
    *,
    task: TaskDTO | None = None,
    task_error: Exception | None = None,
    run=None,
    repository=None,
    story_worker: str | None = None,
) -> SimpleNamespace:
    """Process `job` through the consumer and the real developer node."""
    from src.consumers.engineering import process_engineering_job
    from src.nodes.developer import DeveloperNode

    reads = (
        make_engineering_reads(job, package_build=BUILD, repository_id="repo-1")
        if job["planning_task_id"]
        else None
    )
    api = MagicMock()
    api.patch = AsyncMock()
    api.post = AsyncMock()
    api.get_project = AsyncMock(
        return_value=make_project(config={"modules": ["backend", "tg_bot"]})
    )
    api.get_task = AsyncMock(return_value=task or (reads and reads[0]), side_effect=task_error)
    api.get_run = AsyncMock(return_value=run or (reads and reads[1]))
    api.get_primary_repository = AsyncMock(return_value=repository or make_repository())
    stop = SpawnResult(request_id="req-1", success=False, exit_code=1, output="stop")
    spawn = AsyncMock(return_value=stop)
    send = AsyncMock(return_value=stop)

    async def _invoke(state: dict) -> dict:
        await DeveloperNode().run(state)
        return {"engineering_status": "failed", "errors": ["stop"]}

    subgraph = MagicMock()
    subgraph.ainvoke = AsyncMock(side_effect=_invoke)
    developer_api = MagicMock()
    developer_api.get_project = AsyncMock(return_value=None)
    developer_api.get_primary_repository = AsyncMock(return_value=make_repository())
    developer_api.patch = AsyncMock()
    developer_api.list_story_engineering_runs = AsyncMock(return_value=[])
    # A new task on a reused worker reads its title for the turn preamble.
    developer_api.get_task = api.get_task
    github = MagicMock()
    github.return_value.get_repo = AsyncMock(return_value=SimpleNamespace(default_branch="main"))
    github.return_value.get_ref_sha = AsyncMock(return_value="main-head")

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.subgraphs.engineering.create_engineering_subgraph", return_value=subgraph),
        patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock),
        patch("src.consumers.engineering.get_story_worker", AsyncMock(return_value=story_worker)),
        patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
        patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
        patch("src.consumers.engineering._fail_job", new_callable=AsyncMock) as fail_job,
        patch("src.consumers.engineering.resource_allocator_node") as allocator,
        patch("src.nodes.developer.api_client", developer_api),
        patch("src.nodes.developer.GitHubAppClient", github),
        patch("src.nodes.developer.request_spawn", spawn),
        patch("src.nodes.developer.send_task_to_worker", send),
    ):
        allocator.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})
        await process_engineering_job(job, AsyncMock())
    return SimpleNamespace(api=api, spawn=spawn, send=send, subgraph=subgraph, fail_job=fail_job)


def _assert_names_the_build(task_md: str) -> None:
    section = task_md.split(SECTION, 1)[1].split("\n## ", 1)[0]
    assert "Source directory: `packages/post-ranking`" in section
    assert "`post-ranking`" in section
    assert "`codegen-kit-post-ranking`" in section
    assert "`0.1.0`" in section
    assert "core `2.6.0`" in section
    assert "Python `3.12.0`" in section
    assert f"`{'b' * 40}`" in section
    assert "`requires-python` admitting `3.12.0`" in section
    assert "`protocol_version: 1`" in section
    assert "not proof" in section
    assert "Do not install it" in section


@pytest.mark.asyncio
async def test_a_fresh_worker_is_told_the_stored_build():
    result = await _run(_job())

    result.api.get_task.assert_awaited_once_with("task-build")
    result.send.assert_not_awaited()
    _assert_names_the_build(result.spawn.await_args.kwargs["task_content"])


@pytest.mark.asyncio
async def test_a_reused_story_worker_is_told_the_stored_build():
    result = await _run(_job(), story_worker="dev-story-1")

    result.spawn.assert_not_awaited()
    assert result.send.await_args.kwargs["worker_id"] == "dev-story-1"
    _assert_names_the_build(result.send.await_args.kwargs["task_content"])


@pytest.mark.asyncio
async def test_an_ordinary_task_renders_no_build_section():
    job = _job(planning_task_id="task-plain")
    task, _ = make_engineering_reads(job)

    result = await _run(job, task=task)

    assert SECTION not in result.spawn.await_args.kwargs["task_content"]


@pytest.mark.asyncio
async def test_taskless_work_reads_no_task_and_renders_no_build_section():
    result = await _run(_job(planning_task_id=None, action="fix"))

    result.api.get_task.assert_not_awaited()
    assert SECTION not in result.spawn.await_args.kwargs["task_content"]


def _malformed_task():
    return TaskDTO.model_validate({"id": "task-build", "package_build": {"path": "/abs"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", ["missing", "transport", "malformed"])
async def test_an_unreadable_named_task_fails_the_attempt_before_any_worker(unreadable):
    if unreadable == "missing":
        error = _not_found("tasks/task-build")
    elif unreadable == "transport":
        error = httpx.ConnectError("api unavailable")
    else:
        try:
            _malformed_task()
        except pydantic.ValidationError as invalid:
            error = invalid

    result = await _run(_job(), task_error=error)

    result.subgraph.ainvoke.assert_not_awaited()
    result.spawn.assert_not_awaited()
    run_id, reason, planning_task = result.fail_job.await_args.args
    assert (run_id, planning_task) == ("eng-1", None)
    assert reason.startswith("Planning task task-build unreadable")


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["task_product", "task_story", "run_task", "run_product"])
async def test_a_task_or_run_of_other_work_is_refused_without_touching_it(foreign):
    job = _job()
    task, run = make_engineering_reads(job, package_build=BUILD, repository_id="repo-1")
    if foreign == "task_product":
        task = task.model_copy(update={"project_id": uuid.uuid4()})
    elif foreign == "task_story":
        task = task.model_copy(update={"story_id": "story-other"})
    elif foreign == "run_task":
        run = run.model_copy(update={"task_id": "task-other"})
    else:
        run = run.model_copy(update={"project_id": str(uuid.uuid4())})

    result = await _run(job, task=task, run=run)

    result.subgraph.ainvoke.assert_not_awaited()
    result.spawn.assert_not_awaited()
    run_id, reason, planning_task = result.fail_job.await_args.args
    assert (run_id, planning_task) == ("eng-1", None)
    assert reason.startswith("Planning task refused")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["fix_task", "unmanaged", "other_repository", "foreign"])
async def test_a_build_without_its_feature_type_or_managed_repository_is_refused(owner):
    job = _job()
    task, _ = make_engineering_reads(job, package_build=BUILD, repository_id="repo-1")
    repository = make_repository(id="repo-1")
    if owner == "fix_task":
        task = task.model_copy(update={"type": "fix"})
    elif owner == "unmanaged":
        repository = make_repository(id="repo-1", is_managed=False)
    elif owner == "other_repository":
        repository = make_repository(id="repo-2")
    else:
        repository = make_repository(id="repo-1", project_id=uuid.uuid4())

    result = await _run(job, task=task, repository=repository)

    result.subgraph.ainvoke.assert_not_awaited()
    result.spawn.assert_not_awaited()
    assert "package build task task-build" in result.fail_job.await_args.args[1]
