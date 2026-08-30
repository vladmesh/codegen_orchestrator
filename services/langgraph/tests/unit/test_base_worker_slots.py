"""Tests for bounded parallel consumption in _base.py.

The contract under test: a consumer runs at most `slots` jobs at once, resizes
that bound from runtime configuration without restarting, and never turns a
reclaimed PEL entry into a second run of work that is still live.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.redis_client import StreamMessage


@pytest.fixture()
def mock_api_client():
    with patch("src.consumers._base.api_client") as mock:
        mock.get = AsyncMock()
        mock.get_story = AsyncMock()
        mock.close = AsyncMock()
        yield mock


def _redis(consume):
    redis = MagicMock()
    redis.connect = AsyncMock()
    redis.close = AsyncMock()
    redis.ack = AsyncMock()
    redis.consume = consume
    # No teardown fence, and no live lease held by anyone else.
    redis.redis.eval = AsyncMock(return_value=1)
    redis.redis.exists = AsyncMock(return_value=0)
    redis.redis.zrem = AsyncMock()
    return redis


class TestSlotGate:
    """The gate alone, without a Redis loop around it."""

    @pytest.mark.asyncio()
    async def test_capacity_bounds_concurrent_holders(self):
        from src.consumers._base import SlotGate

        gate = SlotGate(2)
        assert await gate.acquire(0.01)
        assert await gate.acquire(0.01)
        assert not await gate.acquire(0.01)

        gate.release()
        assert await gate.acquire(0.01)

    @pytest.mark.asyncio()
    async def test_zero_capacity_hands_out_nothing(self):
        from src.consumers._base import SlotGate

        gate = SlotGate(0)
        assert not await gate.acquire(0.01)

    @pytest.mark.asyncio()
    async def test_shrinking_never_takes_a_running_slot_back(self):
        from src.consumers._base import SlotGate

        gate = SlotGate(2)
        assert await gate.acquire(0.01)
        assert await gate.acquire(0.01)

        assert gate.resize(1)
        # Both jobs keep running; the gate only stops handing out new slots.
        assert gate.in_flight == 2
        assert not await gate.acquire(0.01)

        gate.release()
        assert not await gate.acquire(0.01)
        gate.release()
        assert await gate.acquire(0.01)

    @pytest.mark.asyncio()
    async def test_growing_releases_a_waiter(self):
        from src.consumers._base import SlotGate

        gate = SlotGate(1)
        assert await gate.acquire(0.01)
        assert not await gate.acquire(0.01)

        assert gate.resize(2)
        assert await gate.acquire(0.01)


class TestConfiguredSlots:
    """Reading the slot count is fail-safe: doubt keeps the current value."""

    @pytest.mark.asyncio()
    async def test_configured_value_is_applied(self, mock_api_client):
        from src.consumers._base import _read_configured_slots

        mock_api_client.get.return_value = {"value": 2}

        assert await _read_configured_slots("engineering.worker_slots", 1, "test") == 2
        mock_api_client.get.assert_awaited_once_with("system-configs/engineering.worker_slots")

    @pytest.mark.asyncio()
    async def test_unreadable_config_keeps_current_value(self, mock_api_client):
        from src.consumers._base import _read_configured_slots

        mock_api_client.get.side_effect = RuntimeError("api down")

        assert await _read_configured_slots("engineering.worker_slots", 2, "test") == 2

    @pytest.mark.asyncio()
    async def test_negative_config_keeps_current_value(self, mock_api_client):
        from src.consumers._base import _read_configured_slots

        mock_api_client.get.return_value = {"value": -1}

        assert await _read_configured_slots("engineering.worker_slots", 1, "test") == 1

    @pytest.mark.asyncio()
    async def test_configured_value_is_clamped_to_the_ceiling(self, mock_api_client):
        from src.consumers._base import MAX_QUEUE_SLOTS, _read_configured_slots

        mock_api_client.get.return_value = {"value": 50}

        assert await _read_configured_slots("k", 1, "test") == MAX_QUEUE_SLOTS

    @pytest.mark.asyncio()
    async def test_zero_is_an_honest_stop_not_a_doubt(self, mock_api_client):
        from src.consumers._base import _read_configured_slots

        mock_api_client.get.return_value = {"value": 0}

        assert await _read_configured_slots("k", 2, "test") == 0


class TestOperatorDrain:
    @pytest.mark.asyncio()
    async def test_drain_leaves_queued_work_unclaimed_while_a_consumer_stays_up(
        self, mock_api_client
    ):
        from src.consumers._base import run_queue_worker

        consume_called = False

        async def consume(*_args, **_kwargs):
            nonlocal consume_called
            consume_called = True
            raise AssertionError("a draining consumer must not read the queue")
            yield  # pragma: no cover

        redis = _redis(consume)

        async def draining():
            return True

        with (
            patch("src.consumers._base.RedisStreamClient", return_value=redis),
            patch("src.consumers._base.SLOT_WAIT_SECONDS", 0.01),
        ):
            worker = asyncio.create_task(
                run_queue_worker("test", "queue", AsyncMock(), is_draining=draining)
            )
            await asyncio.sleep(0.02)
            import src.consumers._base as base

            base._shutdown = True
            await asyncio.wait_for(worker, timeout=2)
            base._shutdown = False

        assert not consume_called

    @pytest.mark.asyncio()
    async def test_unreadable_drain_before_first_read_leaves_available_work_unclaimed(
        self, mock_api_client, monkeypatch
    ):
        """A fresh consumer must not claim while its durable decision is unknown."""
        from src.consumers import engineering
        from src.consumers._base import run_queue_worker

        consume_called = False

        async def consume(*_args, **_kwargs):
            nonlocal consume_called
            consume_called = True
            yield StreamMessage(message_id="1-0", data={"project_id": "project-1"})

        redis = _redis(consume)
        monkeypatch.setattr(engineering, "_last_engineering_consumer_drain", None)
        monkeypatch.setattr(engineering, "_engineering_consumer_drain_read_failed", False)
        monkeypatch.setattr(
            engineering.api_client,
            "get",
            AsyncMock(side_effect=httpx.ConnectError("api unavailable")),
        )

        with (
            patch("src.consumers._base.RedisStreamClient", return_value=redis),
            patch("src.consumers._base.SLOT_WAIT_SECONDS", 0.01),
        ):
            worker = asyncio.create_task(
                run_queue_worker(
                    "test",
                    "queue",
                    AsyncMock(),
                    is_draining=engineering._engineering_consumer_is_draining,
                )
            )
            await asyncio.sleep(0.02)
            import src.consumers._base as base

            base._shutdown = True
            await asyncio.wait_for(worker, timeout=2)
            base._shutdown = False

        assert not consume_called
        redis.ack.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_drain_after_an_idle_consumer_reserved_a_slot_leaves_new_entry_in_pel(
        self, mock_api_client
    ):
        """A drain can arrive while XREADGROUP is blocked after slot reservation."""
        from src.consumers._base import run_queue_worker

        idle = asyncio.Event()
        release_entry = asyncio.Event()
        drained = False

        async def consume(*_args, **_kwargs):
            idle.set()
            yield None
            await release_entry.wait()
            yield StreamMessage(message_id="1-0", data={"project_id": "p"})
            while True:
                yield None
                await asyncio.sleep(0.01)

        redis = _redis(consume)
        process = AsyncMock()

        async def is_draining():
            return drained

        with (
            patch("src.consumers._base.RedisStreamClient", return_value=redis),
            patch("src.consumers._base.SLOT_WAIT_SECONDS", 0.01),
        ):
            worker = asyncio.create_task(
                run_queue_worker("test", "queue", process, is_draining=is_draining)
            )
            await asyncio.wait_for(idle.wait(), timeout=1)
            drained = True
            release_entry.set()
            await asyncio.sleep(0.03)
            import src.consumers._base as base

            base._shutdown = True
            await asyncio.wait_for(worker, timeout=2)
            base._shutdown = False

        process.assert_not_awaited()
        redis.ack.assert_not_awaited()


class TestParallelConsumption:
    """Two projects work at the same time; one slot stays sequential."""

    @pytest.mark.asyncio()
    async def test_two_projects_overlap_with_two_slots(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        messages = [
            StreamMessage(message_id="1-0", data={"task_id": "run-1", "project_id": "project-1"}),
            StreamMessage(message_id="2-0", data={"task_id": "run-2", "project_id": "project-2"}),
        ]

        async def consume(*_args, **_kwargs):
            for message in messages:
                yield message
            # Keep the generator open so the loop cannot exit before both jobs
            # have had the chance to overlap.
            while True:
                yield None
                await asyncio.sleep(0)

        running: set[str] = set()
        peak = 0
        release = asyncio.Event()

        async def process(data, _redis):
            nonlocal peak
            running.add(data["task_id"])
            peak = max(peak, len(running))
            if len(running) == 2:
                release.set()
            await release.wait()
            running.discard(data["task_id"])
            return {"status": "completed"}

        redis = _redis(consume)

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            worker = asyncio.create_task(
                run_queue_worker("test", "queue", process, default_slots=2)
            )
            await asyncio.wait_for(release.wait(), timeout=2)
            await asyncio.sleep(0.05)
            import src.consumers._base as base

            base._shutdown = True
            await asyncio.wait_for(worker, timeout=2)
            base._shutdown = False

        assert peak == 2
        acked = {call.args[2] for call in redis.ack.await_args_list}
        assert acked == {"1-0", "2-0"}

    @pytest.mark.asyncio()
    async def test_one_slot_stays_sequential(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        messages = [
            StreamMessage(message_id="1-0", data={"task_id": "run-1", "project_id": "project-1"}),
            StreamMessage(message_id="2-0", data={"task_id": "run-2", "project_id": "project-2"}),
        ]

        async def consume(*_args, **_kwargs):
            for message in messages:
                yield message

        running = 0
        peak = 0

        async def process(_data, _redis):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
            return {"status": "completed"}

        redis = _redis(consume)

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            await asyncio.wait_for(
                run_queue_worker("test", "queue", process, default_slots=1), timeout=2
            )

        assert peak == 1
        assert redis.ack.await_count == 2


class TestReclaimGuards:
    """A reclaimed entry never becomes a second run of live work."""

    @pytest.mark.asyncio()
    async def test_entry_in_flight_here_is_not_taken_twice(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        message = StreamMessage(
            message_id="1-0", data={"task_id": "run-1", "project_id": "project-1"}
        )
        reclaimed = StreamMessage(
            message_id="1-0",
            data={"task_id": "run-1", "project_id": "project-1"},
            reclaimed=True,
        )

        async def consume(*_args, **_kwargs):
            yield message
            yield reclaimed
            while True:
                yield None
                await asyncio.sleep(0)

        starts = 0
        release = asyncio.Event()

        async def process(_data, _redis):
            nonlocal starts
            starts += 1
            await release.wait()
            return {"status": "completed"}

        redis = _redis(consume)

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            worker = asyncio.create_task(
                run_queue_worker("test", "queue", process, default_slots=2)
            )
            await asyncio.sleep(0.05)
            assert starts == 1
            release.set()
            await asyncio.sleep(0.05)
            import src.consumers._base as base

            base._shutdown = True
            await asyncio.wait_for(worker, timeout=2)
            base._shutdown = False

        assert starts == 1

    @pytest.mark.asyncio()
    async def test_reclaimed_entry_of_a_live_project_is_left_alone(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        reclaimed = StreamMessage(
            message_id="9-0",
            data={"task_id": "run-9", "project_id": "project-9"},
            reclaimed=True,
        )

        async def consume(*_args, **_kwargs):
            yield reclaimed

        async def process(_data, _redis):
            raise AssertionError("a live project's entry must not be taken over")

        redis = _redis(consume)
        # A lease is held: some worker is alive on project-9.
        redis.redis.eval = AsyncMock(return_value=1)

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            await asyncio.wait_for(run_queue_worker("test", "queue", process), timeout=2)

        redis.ack.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_reclaimed_entry_of_a_dead_owner_is_taken_over(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        reclaimed = StreamMessage(
            message_id="9-0",
            data={"task_id": "run-9", "project_id": "project-9"},
            reclaimed=True,
        )

        async def consume(*_args, **_kwargs):
            yield reclaimed

        processed = []

        async def process(data, _redis):
            processed.append(data["task_id"])
            return {"status": "completed"}

        redis = _redis(consume)
        # ZCARD 0 on the liveness read, then the job's own lease registers.
        redis.redis.eval = AsyncMock(side_effect=[0, 1])

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            await asyncio.wait_for(run_queue_worker("test", "queue", process), timeout=2)

        assert processed == ["run-9"]
        redis.ack.assert_awaited_once_with("queue", "capability-workers", "9-0")

    @pytest.mark.asyncio()
    async def test_unreadable_lease_fails_closed(self, mock_api_client):
        from src.consumers._base import run_queue_worker

        mock_api_client.get.return_value = {"status": "running"}

        reclaimed = StreamMessage(
            message_id="9-0",
            data={"task_id": "run-9", "project_id": "project-9"},
            reclaimed=True,
        )

        async def consume(*_args, **_kwargs):
            yield reclaimed

        async def process(_data, _redis):
            raise AssertionError("an unreadable lease is not proof the owner is dead")

        redis = _redis(consume)
        redis.redis.eval = AsyncMock(side_effect=RuntimeError("redis broke"))

        with patch("src.consumers._base.RedisStreamClient", return_value=redis):
            await asyncio.wait_for(run_queue_worker("test", "queue", process), timeout=2)

        redis.ack.assert_not_awaited()


class TestLiveWorkActive:
    @pytest.mark.asyncio()
    async def test_live_lease_is_read_against_redis_time(self):
        from src.consumers._live_work import live_work_active

        redis = MagicMock()
        redis.redis.eval = AsyncMock(return_value=1)

        assert await live_work_active(redis, "project-1")
        script = redis.redis.eval.await_args.args[0]
        assert "TIME" in script
        assert "ZREMRANGEBYSCORE" in script
        assert "ZCARD" in script

    @pytest.mark.asyncio()
    async def test_no_lease_means_not_live(self):
        from src.consumers._live_work import live_work_active

        redis = MagicMock()
        redis.redis.eval = AsyncMock(return_value=0)

        assert not await live_work_active(redis, "project-1")
