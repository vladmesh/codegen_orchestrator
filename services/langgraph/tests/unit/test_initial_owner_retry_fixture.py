"""The CI recovery proof keeps its time bound without a pytest-only plugin."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def recovery():
    path = Path(__file__).resolve().parents[1] / "service/test_initial_owner_retry.py"
    spec = importlib.util.spec_from_file_location("initial_owner_retry_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_route", [None, "retry", "poll", "infrastructure", "secret"])
async def test_batch_prepares_every_route_waits_once_and_cleans_up(
    recovery, monkeypatch, failed_route
):
    routes = ["retry", "poll", "infrastructure", "secret"]
    events = []
    failure = AssertionError("controlled preparation failure")
    next_project = iter(routes)

    @asynccontextmanager
    async def project_context():
        project = next(next_project)
        events.append(("open", project))
        try:
            yield project
        finally:
            events.append(("close", project))

    async def prepare(project, route):
        assert project == route
        events.append(("prepare", route))
        if route == failed_route:
            raise failure
        return route

    async def wait(seconds):
        events.append(("wait", seconds))

    monkeypatch.setattr(recovery, "public_project_context", project_context, raising=False)
    monkeypatch.setattr(recovery, "_prepare_native_recovery", prepare, raising=False)
    monkeypatch.setattr(recovery.asyncio, "sleep", wait)
    async with recovery.prepared_recovery_batch(routes) as batch:
        assert list(batch) == routes
        assert batch == {route: failure if route == failed_route else route for route in routes}
        assert events == [
            event for route in routes for event in [("open", route), ("prepare", route)]
        ] + [("wait", 61)]
    assert events[-4:] == [("close", route) for route in reversed(routes)]


@pytest.mark.asyncio
async def test_preparation_failure_does_not_hide_later_scenario_results(recovery, monkeypatch):
    failure = AssertionError("retry preparation failed")
    batch = {"retry": failure, "poll": "prepared poll", "secret": "prepared secret"}
    completed = []

    async def finish(prepared, redis):
        completed.append(prepared)
        if prepared == "prepared poll":
            raise AssertionError("poll completion failed")

    monkeypatch.setattr(recovery, "_finish_native_recovery", finish, raising=False)
    with pytest.raises(AssertionError, match="retry preparation failed"):
        await recovery.test_native_exhaustion_notice_and_po_retry(batch, None, "retry")
    with pytest.raises(AssertionError, match="poll completion failed"):
        await recovery.test_native_exhaustion_notice_and_po_retry(batch, None, "poll")
    await recovery.test_native_exhaustion_notice_and_po_retry(batch, None, "secret")
    assert completed == ["prepared poll", "prepared secret"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_during", ["prepare", "wait", "finish"])
async def test_batch_cleans_acquired_projects_on_cancellation(recovery, monkeypatch, exit_during):
    events = []
    projects = iter(["retry", "poll"])

    @asynccontextmanager
    async def project_context():
        project = next(projects)
        events.append(("open", project))
        try:
            yield project
        finally:
            events.append(("close", project))

    async def prepare(project, route):
        if exit_during == "prepare" and route == "poll":
            raise asyncio.CancelledError
        return project

    async def wait(seconds):
        if exit_during == "wait":
            raise asyncio.CancelledError

    monkeypatch.setattr(recovery, "public_project_context", project_context)
    monkeypatch.setattr(recovery, "_prepare_native_recovery", prepare)
    monkeypatch.setattr(recovery.asyncio, "sleep", wait)
    with pytest.raises(asyncio.CancelledError):
        async with recovery.prepared_recovery_batch(["retry", "poll"]):
            raise asyncio.CancelledError
    assert events == [("open", "retry"), ("open", "poll"), ("close", "poll"), ("close", "retry")]


@pytest.mark.asyncio
async def test_public_project_cleans_up_when_setup_fails(recovery, monkeypatch):
    from tests.service import test_public_deploy as public

    api = AsyncMock()
    api.post.side_effect = [None, {"id": "acquired-project"}, AssertionError("repository setup")]
    delete = AsyncMock()
    monkeypatch.setenv("TEST_API_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(public, "LanggraphAPIClient", lambda: api)
    monkeypatch.setattr(public, "delete_public_project", delete)
    with pytest.raises(AssertionError, match="repository setup"):
        async with recovery.public_project_context():
            pytest.fail("Failed setup must not yield")
    delete.assert_awaited_once_with(api, "acquired-project")
    api.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_continues_after_project_setup_failure(recovery, monkeypatch):
    projects = iter(["retry", "poll"])
    closed = []

    @asynccontextmanager
    async def project_context():
        project = next(projects)
        try:
            if project == "retry":
                raise AssertionError("project setup")
            yield project
        finally:
            closed.append(project)

    async def prepare(project, route):
        return project

    monkeypatch.setattr(recovery, "public_project_context", project_context)
    monkeypatch.setattr(recovery, "_prepare_native_recovery", prepare)
    monkeypatch.setattr(recovery.asyncio, "sleep", AsyncMock())
    async with recovery.prepared_recovery_batch(["retry", "poll"]) as cases:
        assert isinstance(cases["retry"], AssertionError)
        assert cases["poll"] == "poll"
        assert closed == ["retry"]
    assert closed == ["retry", "poll"]


@pytest.mark.asyncio
async def test_preparation_restores_policy_after_producer_failure(recovery, monkeypatch):
    api = AsyncMock()
    api.get.side_effect = [{"owner_id": "owner"}, {"telegram_id": 123}]
    monkeypatch.setattr(
        recovery, "failed_source", AsyncMock(side_effect=AssertionError("producer"))
    )
    with pytest.raises(AssertionError, match="producer"):
        await recovery._prepare_native_recovery((api, None, "project", "story"), "retry")
    assert [call.kwargs["json"]["value"] for call in api.post.await_args_list] == [1, 3]


@pytest.mark.asyncio
async def test_completion_restores_policy_after_scheduler_failure(recovery, monkeypatch):
    api = AsyncMock()
    prepared = recovery.PreparedRecovery("project", "story", {}, "source", None, {}, {})

    def fail(mode, story):
        raise AssertionError("scheduler")

    monkeypatch.setattr(recovery, "scheduler", fail)
    with pytest.raises(AssertionError, match="scheduler"):
        await recovery._complete_native_recovery(api, None, prepared, None)
    api.post.assert_awaited_once_with(
        "system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
    )


@pytest.mark.asyncio
async def test_native_recovery_proof_cancels_at_its_deadline(recovery, monkeypatch):
    timeout = asyncio.timeout
    bounds = []
    exited = asyncio.Event()

    def fast_deadline(seconds):
        bounds.append(seconds)
        return timeout(0.01)

    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(recovery.asyncio, "timeout", fast_deadline)
    monkeypatch.setattr(recovery, "_finish_native_recovery", blocked)
    with pytest.raises(TimeoutError):
        await recovery.test_native_exhaustion_notice_and_po_retry({"retry": None}, None, "retry")
    assert bounds == [180]
    assert exited.is_set()
