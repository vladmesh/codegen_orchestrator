"""Wake hints shorten a periodic loop's wait; the interval stays the backstop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from _wake_doubles import Broker, Cycles, Timers, settle
import pytest

from shared.contracts.queues.scheduler_wake import SCHEDULER_WAKE_CHANNEL, SchedulerWakeTarget
from shared.redis.scheduler_wake import publish_scheduler_wake

STORY_COMPLETION = SchedulerWakeTarget.STORY_COMPLETION
TASK_DISPATCHER = SchedulerWakeTarget.TASK_DISPATCHER
LIFECYCLE_SUPERVISION = SchedulerWakeTarget.LIFECYCLE_SUPERVISION


def receivers(**events: asyncio.Event) -> dict[SchedulerWakeTarget, asyncio.Event]:
    """One event per target; the named ones are the test's, the rest are fresh."""
    return {target: events.get(target.value, asyncio.Event()) for target in SchedulerWakeTarget}


def start_loop(cycles: Cycles, timers: Timers, wake: asyncio.Event, *, interval=lambda: 30):
    from src import runtime

    client = MagicMock(connect=AsyncMock(), close=AsyncMock())
    log = MagicMock()
    task = asyncio.create_task(
        runtime.periodic_loop(
            interval=interval,
            cycle=cycles,
            logger=log,
            started_event="started",
            cycle_event="cycle",
            error_event="failed",
            stopped_event="stopped",
            redis_factory=lambda: client,
            sleep=timers.sleep,
            wake=wake,
        )
    )
    return task, client, log


def start_listener(broker: Broker, timers: Timers, wakes, monkeypatch):
    from src.tasks import scheduler_wake_listener

    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: 30)
    return asyncio.create_task(
        scheduler_wake_listener.scheduler_wake_listener(
            wakes, redis_factory=broker.client, sleep=timers.sleep
        )
    )


async def stop(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def publish(broker: Broker, target=STORY_COMPLETION) -> int:
    publisher = broker.client()
    await publisher.connect()
    return await publish_scheduler_wake(publisher, target)


def other_tasks() -> set[asyncio.Task]:
    return asyncio.all_tasks() - {asyncio.current_task()}


@pytest.mark.asyncio
async def test_a_wake_while_idle_runs_the_next_cycle_before_the_backstop():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    loop, _client, _log = start_loop(cycles, timers, wake)
    await settle()
    assert (cycles.runs, timers.waiting) == (1, 1)

    wake.set()
    await settle()

    assert cycles.runs == 2
    assert timers.cancelled == 1
    assert timers.waiting == 1
    assert not wake.is_set()
    await stop(loop)


@pytest.mark.asyncio
async def test_without_a_wake_the_backstop_runs_the_next_cycle_and_rereads_the_interval():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    intervals = iter([30, 5, 7])
    loop, _client, _log = start_loop(cycles, timers, wake, interval=lambda: next(intervals))
    await settle()
    assert timers.requested == [5]

    timers.fire()
    await settle()

    assert cycles.runs == 2
    assert timers.requested == [5, 7]
    await stop(loop)


@pytest.mark.asyncio
async def test_wakes_during_a_cycle_run_exactly_one_prompt_follow_up():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    cycles.hold = release = asyncio.Event()
    loop, _client, _log = start_loop(cycles, timers, wake)
    await settle()
    assert cycles.active == 1

    for _ in range(3):
        wake.set()
        await settle()
    release.set()
    await settle()

    assert cycles.runs == 2
    assert cycles.max_active == 1
    assert timers.waiting == 1
    assert timers.requested == [30, 30]
    await stop(loop)


@pytest.mark.asyncio
async def test_a_wake_just_before_the_wait_registers_runs_one_follow_up():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    calls = 0

    def interval() -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            wake.set()
        return 30

    loop, _client, _log = start_loop(cycles, timers, wake, interval=interval)
    await settle()

    assert cycles.runs == 2
    assert timers.waiting == 1
    await stop(loop)


@pytest.mark.asyncio
async def test_a_burst_of_wakes_coalesces_without_overlap_or_queued_tasks():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    loop, _client, _log = start_loop(cycles, timers, wake)
    await settle()
    baseline = len(other_tasks())

    for _ in range(100):
        wake.set()
    await settle()

    assert cycles.runs == 2
    assert cycles.max_active == 1
    assert len(other_tasks()) == baseline
    await stop(loop)


@pytest.mark.asyncio
async def test_a_failed_cycle_keeps_both_the_wake_and_the_backstop():
    cycles, timers, wake = Cycles(fail_first=True), Timers(), asyncio.Event()
    loop, _client, log = start_loop(cycles, timers, wake)
    await settle()
    log.exception.assert_called_once_with("failed")

    wake.set()
    await settle()
    assert cycles.runs == 2

    timers.fire()
    await settle()
    assert cycles.runs == 3
    await stop(loop)


@pytest.mark.asyncio
async def test_cancelling_a_waiting_loop_closes_redis_once_and_leaves_no_helper_task():
    cycles, timers, wake = Cycles(), Timers(), asyncio.Event()
    loop, client, _log = start_loop(cycles, timers, wake)
    await settle()

    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    client.close.assert_awaited_once()
    assert timers.cancelled == 1
    assert other_tasks() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", list(SchedulerWakeTarget))
async def test_a_published_hint_wakes_only_its_own_loop(monkeypatch, target):
    broker, wakes = Broker(), receivers()
    listener = start_listener(broker, Timers(), wakes, monkeypatch)
    await broker.subscribed()

    assert await publish(broker, target) == 1
    await settle()

    assert {name for name, wake in wakes.items() if wake.is_set()} == {target}
    await stop(listener)


@pytest.mark.asyncio
async def test_rejected_payloads_neither_wake_nor_stop_the_listener(monkeypatch):
    from src.tasks import scheduler_wake_listener

    log = MagicMock()
    monkeypatch.setattr(scheduler_wake_listener, "logger", log)
    broker, wake = Broker(), asyncio.Event()
    listener = start_listener(broker, Timers(), receivers(story_completion=wake), monkeypatch)
    await broker.subscribed()

    for payload in (
        '{"target": "pr_ci"}',
        '{"target": "story_completion", "story_id": "story-1"}',
        "not json",
        b'{"target": "story_completion"}',
    ):
        broker.deliver(SCHEDULER_WAKE_CHANNEL, payload)
    await settle()

    assert not wake.is_set()
    rejected = [
        c for c in log.warning.call_args_list if c.args == ("scheduler_wake_hint_rejected",)
    ]
    assert len(rejected) == 4
    broker.deliver(SCHEDULER_WAKE_CHANNEL, '{"target": "story_completion"}')
    await settle()
    assert wake.is_set()
    await stop(listener)


@pytest.mark.asyncio
async def test_a_listener_outage_reconnects_after_the_interval_while_the_backstop_runs(
    monkeypatch,
):
    broker, wake = Broker(), asyncio.Event()
    broker.connect_failures = 1
    listener_timers, cycles, loop_timers = Timers(), Cycles(), Timers()
    listener = start_listener(
        broker, listener_timers, receivers(story_completion=wake), monkeypatch
    )
    loop, _client, _log = start_loop(cycles, loop_timers, wake)
    await settle()

    assert cycles.runs == 1
    assert listener_timers.requested == [30]
    assert broker.clients[0].closed == 1
    loop_timers.fire()
    await settle()
    assert cycles.runs == 2
    assert loop_timers.requested == [30, 30]

    listener_timers.fire()
    await broker.subscribed()
    await publish(broker)
    await settle()
    assert cycles.runs == 3
    await stop(listener, loop)


@pytest.mark.asyncio
async def test_a_dropped_subscription_closes_its_resources_and_resubscribes(monkeypatch):
    broker, wake = Broker(), asyncio.Event()
    timers = Timers()
    listener = start_listener(broker, timers, receivers(story_completion=wake), monkeypatch)
    first = await broker.subscribed()

    broker.drop_connections()
    await settle()

    assert first.closed == 1
    assert broker.clients[0].closed == 1
    assert timers.requested == [30]
    timers.fire()
    await settle()
    second = await broker.subscribed()
    assert second is not first
    await publish(broker)
    await settle()
    assert wake.is_set()
    await stop(listener)


@pytest.mark.asyncio
async def test_cancelling_the_listener_closes_pubsub_and_client_once(monkeypatch):
    broker = Broker()
    listener = start_listener(broker, Timers(), receivers(), monkeypatch)
    pubsub = await broker.subscribed()

    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener

    assert pubsub.closed == 1
    assert [client.closed for client in broker.clients] == [1]
    assert other_tasks() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wakes", "missing"),
    [
        ({}, "story_completion"),
        (
            {TASK_DISPATCHER: asyncio.Event(), LIFECYCLE_SUPERVISION: asyncio.Event()},
            "story_completion",
        ),
        (
            {STORY_COMPLETION: asyncio.Event(), LIFECYCLE_SUPERVISION: asyncio.Event()},
            "task_dispatcher",
        ),
        (
            {STORY_COMPLETION: asyncio.Event(), TASK_DISPATCHER: asyncio.Event()},
            "lifecycle_supervision",
        ),
    ],
)
async def test_the_listener_refuses_a_target_without_a_receiver(wakes, missing):
    from src.tasks.scheduler_wake_listener import scheduler_wake_listener

    with pytest.raises(ValueError, match=missing):
        await scheduler_wake_listener(wakes)
