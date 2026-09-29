"""Lease uncertainty and cancellation transitions without wall-clock sleeps."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConnectionError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)
from structlog import wrap_logger
from structlog.testing import capture_logs

from src.consumers import _live_work as work


@pytest.fixture
def client():
    redis = MagicMock()
    redis.redis.eval = AsyncMock(return_value=1)
    redis.terminal_reply = AsyncMock(return_value=1)
    redis.confirmed_acks = []

    async def evaluate(script, *args):
        if "'XACK'" in script:
            reply = await redis.terminal_reply(*args)
            if reply == 1:
                redis.confirmed_acks.append(args[-1])
            return reply
        return 1

    redis.redis.eval.side_effect = evaluate
    redis.redis.exists = AsyncMock(return_value=False)
    redis.redis.set = AsyncMock()
    redis.redis.zrem = AsyncMock()
    redis.ack = AsyncMock()
    return redis


async def execute(client, process):
    return await work.execute_live_work(
        client,
        queue="queue",
        group="group",
        message_id="1-0",
        project_id="project",
        process=process,
    )


@pytest.mark.parametrize("error", [ConnectionError, RedisTimeoutError])
async def test_native_transient_watch_failure_recovers_without_cancelling_process(
    client,
    monkeypatch,
    error,
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    recovered = asyncio.Event()
    attempts = 0

    async def check(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error("synthetic connection fault")
        recovered.set()
        return False

    client.redis.exists.side_effect = check
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        await asyncio.wait_for(recovered.wait(), 1)
        return {"status": "passed"}

    assert await execute(client, process) == {"status": "passed"}
    assert calls == 1
    assert attempts >= 2
    client.terminal_reply.assert_awaited_once()
    assert client.confirmed_acks == ["1-0"]
    client.redis.set.assert_not_awaited()


async def test_completion_during_native_outage_waits_without_ack_or_reexecution(
    client, monkeypatch
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    failed = asyncio.Event()
    recovered = False

    async def check(*args):
        if not recovered:
            failed.set()
            raise ConnectionError("synthetic")
        return 1

    client.terminal_reply.side_effect = check
    process = AsyncMock(return_value={"status": "passed"})
    task = asyncio.create_task(execute(client, process))
    try:
        await asyncio.wait_for(failed.wait(), 1)
        assert not task.done()
        assert client.terminal_reply.await_count >= 1
        assert client.confirmed_acks == []
        recovered = True
        assert await asyncio.wait_for(task, 1) == {"status": "passed"}
        process.assert_awaited_once()
        assert client.terminal_reply.await_count >= 2
        assert client.confirmed_acks == ["1-0"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fault", ["removed", "defect"])
async def test_owner_loss_cancels_process_but_never_acks_as_success(client, monkeypatch, fault):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    if fault == "removed":
        client.redis.eval.side_effect = [1, 0]
    else:
        client.redis.exists.side_effect = ResponseError("synthetic non-transient defect")
    cancelled = asyncio.Event()

    async def process():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await asyncio.wait_for(execute(client, process), 1)
    assert cancelled.is_set()
    client.ack.assert_not_awaited()
    client.redis.set.assert_awaited()


@pytest.mark.parametrize("recover_at, succeeds", [(50, True), (60, False), (70, False)])
async def test_retry_deadline_never_moves_on_failed_attempts(
    client, monkeypatch, recover_at, succeeds
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(work, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(work.asyncio, "sleep", sleep)
    calls = []

    async def check(*args):
        calls.append(clock.now)
        if clock.now < recover_at:
            raise ConnectionError("offline")
        return False

    client.redis.exists.side_effect = check
    state = work._LeaseState(60)
    if succeeds:
        assert not await work._confirm_live_work(client, "project", "token", state)
        assert state.deadline == 110
    else:
        with pytest.raises(work.LiveWorkOwnershipError, match="exhausted"):
            await work._confirm_live_work(client, "project", "token", state)
        assert state.deadline == 60
        client.redis.eval.assert_not_awaited()
    assert max(calls) <= 50


async def test_successful_network_reply_at_deadline_cannot_extend_ownership(client, monkeypatch):
    clock = SimpleNamespace(now=59.0)
    monkeypatch.setattr(work, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def renew(*args):
        clock.now = 60.0
        return 1

    client.redis.eval.side_effect = renew
    state = work._LeaseState(60)
    with pytest.raises(work.LiveWorkOwnershipError, match="exhausted"):
        await work._confirm_live_work(client, "project", "token", state)
    assert state.deadline == 60


async def test_ack_reply_at_deadline_exposes_applied_ack_without_claiming_ownership(
    client,
    monkeypatch,
):
    clock = SimpleNamespace(now=59.0)
    monkeypatch.setattr(work, "time", SimpleNamespace(monotonic=lambda: clock.now))
    # Earlier worker tests enable first-use logger caching. Capture a fresh
    # native logger so that its processors follow capture_logs in either order.
    monkeypatch.setattr(work, "logger", wrap_logger(None, cache_logger_on_first_use=False))

    async def ack_reply(*args):
        clock.now = 60.0
        return 1

    client.terminal_reply.side_effect = ack_reply
    state = work._LeaseState(60, result={"status": "passed"})
    with capture_logs() as events:
        with pytest.raises(work.LiveWorkOwnershipError, match="exhausted"):
            await work._commit_live_work(client, "project", "token", state, "queue", "group", "1-0")
    assert state.deadline == 60
    client.terminal_reply.assert_awaited_once()
    assert "lease_uncertainty_exhausted" in client.redis.set.await_args.args
    assert any(e.get("event") == "live_work_ack_reply_after_deadline" for e in events)


async def test_blocked_network_calls_exhaust_ownership_and_leave_pending(client, monkeypatch):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_SECONDS", 0.06)

    async def blocked(*args):
        await asyncio.Event().wait()

    client.redis.exists.side_effect = blocked
    process = AsyncMock(side_effect=blocked)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execute(client, process), 0.5)
    client.ack.assert_not_awaited()
    assert "lease_uncertainty_exhausted" in client.redis.set.await_args.args
    assert not any(t.get_name().startswith("live-work-watch:") for t in asyncio.all_tasks())


async def test_shutdown_during_retry_cancels_and_awaits_watch_without_ack(client, monkeypatch):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    uncertain = asyncio.Event()

    async def offline(*args):
        uncertain.set()
        raise ConnectionError("offline")

    client.redis.exists.side_effect = offline
    client.terminal_reply.return_value = -3

    async def process():
        await asyncio.Event().wait()

    task = asyncio.create_task(execute(client, process))
    await asyncio.wait_for(uncertain.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    client.ack.assert_not_awaited()
    client.redis.zrem.assert_awaited_once()
    assert not any(t.get_name().startswith("live-work-watch:") for t in asyncio.all_tasks())


async def test_suppressed_cancellation_cannot_turn_lease_loss_into_success(client, monkeypatch):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    client.redis.eval.side_effect = [1, 0]

    async def process():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return work.live_work_settled({"status": "passed"})

    with pytest.raises(work.LiveWorkOwnershipError, match="lease_lost"):
        await asyncio.wait_for(execute(client, process), 1)
    client.ack.assert_not_awaited()


async def test_watch_teardown_with_unproven_external_stop_never_acks(client, monkeypatch):
    from shared.clients.github import WorkflowCancellationUnprovenError

    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    client.redis.exists.return_value = True

    async def process():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise WorkflowCancellationUnprovenError("stop still unproven") from None

    with pytest.raises(WorkflowCancellationUnprovenError):
        await asyncio.wait_for(execute(client, process), 1)
    client.ack.assert_not_awaited()
    assert "workflow_cancellation_unproven" in client.redis.set.await_args.args
    client.redis.set.assert_awaited()


async def test_cleanup_failure_cannot_mask_unproven_external_cancellation(client):
    from shared.clients.github import WorkflowCancellationUnprovenError

    client.redis.set.side_effect = ConnectionError("marker unavailable")
    client.redis.zrem.side_effect = ConnectionError("cleanup unavailable")

    async def process():
        raise WorkflowCancellationUnprovenError("external stop unproven")

    with pytest.raises(WorkflowCancellationUnprovenError):
        await execute(client, process)
    client.ack.assert_not_awaited()


async def test_shutdown_during_completion_retry_keeps_entry_pending(client, monkeypatch):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.01)
    uncertain = asyncio.Event()

    async def offline(*args):
        uncertain.set()
        raise ConnectionError("offline")

    client.terminal_reply.side_effect = offline
    process = AsyncMock(return_value={"status": "passed"})
    task = asyncio.create_task(execute(client, process))
    await asyncio.wait_for(uncertain.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    process.assert_awaited_once()
    client.ack.assert_not_awaited()
    client.redis.zrem.assert_awaited_once()
    assert not any(t.get_name().startswith("live-work-watch:") for t in asyncio.all_tasks())


async def test_cleanup_failures_cannot_mask_unsettled_teardown_result(client):
    client.redis.exists.return_value = True
    client.terminal_reply.return_value = -1
    client.redis.set.side_effect = ConnectionError("marker unavailable")
    client.redis.zrem.side_effect = ConnectionError("cleanup unavailable")
    process = AsyncMock(return_value=work.live_work_unsettled({"status": "failed"}))
    with pytest.raises(work.LiveWorkResultUnsettledError):
        await execute(client, process)
    client.ack.assert_not_awaited()


@pytest.mark.parametrize(
    "reply, reason", [(-2, "lease_lost"), (-1, "cancel_settlement_failed"), (0, "ack_uncertain")]
)
async def test_cancellation_cannot_override_a_terminal_failure_during_marker_write(
    client,
    reply,
    reason,
):
    client.terminal_reply.return_value = reply
    marking = asyncio.Event()
    writes = 0

    async def marker(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            marking.set()
            await asyncio.Event().wait()

    client.redis.set.side_effect = marker
    process = AsyncMock(return_value=work.live_work_unsettled({"status": "failed"}))
    task = asyncio.create_task(execute(client, process))
    try:
        await asyncio.wait_for(marking.wait(), 1)
        # Even a later successful Redis reply must not override the known failure.
        client.terminal_reply.return_value = 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        client.terminal_reply.assert_awaited_once()
        assert reason in client.redis.set.await_args.args
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_result_survives_cancellation_while_stopping_watch(client, monkeypatch):
    stopping = asyncio.Event()
    stop = work._stop_watch
    calls = 0

    async def stop_once(watch):
        nonlocal calls
        calls += 1
        if calls == 1:
            stopping.set()
            await asyncio.Event().wait()
        await stop(watch)

    monkeypatch.setattr(work, "_stop_watch", stop_once)
    client.terminal_reply.return_value = -1
    task = asyncio.create_task(
        execute(
            client,
            AsyncMock(return_value=work.live_work_unsettled({"status": "failed"})),
        )
    )
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        # Completed, unsettled, cancelled flags reach the same atomic boundary.
        assert client.terminal_reply.await_args.args[-5:-2] == (1, 0, 1)
        assert "cancel_settlement_failed" in client.redis.set.await_args.args
        assert not any(t.get_name().startswith("live-work-watch:") for t in asyncio.all_tasks())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stopping_watch_joins_child_without_swallowing_owner_cancellation():
    running, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def watch():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()

    child = asyncio.create_task(watch())
    await asyncio.wait_for(running.wait(), 1)
    owner = asyncio.create_task(work._stop_watch(child))
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        owner.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1)
        assert child.done()
    finally:
        release.set()
        owner.cancel()
        child.cancel()
        await asyncio.gather(owner, child, return_exceptions=True)


async def test_owner_cancellation_during_watch_cleanup_preserves_unproven_workflow(
    client, monkeypatch
):
    from shared.clients.github import WorkflowCancellationUnprovenError

    running, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def watch(*args):
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()

    async def process():
        await running.wait()
        raise WorkflowCancellationUnprovenError("external stop unproven")

    monkeypatch.setattr(work, "_cancel_on_live_teardown", watch)
    task = asyncio.create_task(execute(client, process))
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(WorkflowCancellationUnprovenError):
            await asyncio.wait_for(task, 1)
        assert "workflow_cancellation_unproven" in client.redis.set.await_args.args
        client.terminal_reply.assert_not_awaited()
        client.redis.zrem.assert_awaited_once()
        assert not any(t.get_name().startswith("live-work-watch:") for t in asyncio.all_tasks())
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("error_type", [AuthenticationError, AuthorizationError])
async def test_native_access_defect_is_not_retried_as_connection_uncertainty(
    client,
    monkeypatch,
    error_type,
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(work, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(work.asyncio, "sleep", sleep)
    attempt = AsyncMock(side_effect=error_type("synthetic access defect"))
    with pytest.raises(error_type):
        await work._within_live_lease(work._LeaseState(60), attempt)
    attempt.assert_awaited_once()
    assert clock.now == 0
