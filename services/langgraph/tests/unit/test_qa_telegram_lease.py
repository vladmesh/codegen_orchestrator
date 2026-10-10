"""The shared QA Telegram identity's exclusive hold: one holder, released only when use ended.

These drive the real `TelegramIdentityLease` and its Lua scripts over an in-memory
Redis. Time is a counter the waits advance; the renewal watchdog fires only when a
test lets it. The same adapter against a real Redis runs in the LangGraph service leg
(`tests/service/test_qa_telegram_identity_lease.py`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fakeredis.aioredis import FakeRedis
import pytest

from src.consumers._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityOwnershipLost,
    TelegramIdentityLease,
)

ACCOUNT = 8202532144
BUYER = Holder(HolderKind.SYNTHETIC_BUYER, "s1487-buyer-001", "buyer:order")
QA = Holder(HolderKind.NATIVE_QA, "qa-run-1", "exploratory")


class Time:
    """Seconds and a UTC moment that move only when a wait sleeps."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def wall(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return datetime(2026, 10, 10, 12, tzinfo=UTC) + timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds
        await asyncio.sleep(0)


class Renewals:
    """The watchdog's wait, released one renewal at a time by the test."""

    def __init__(self) -> None:
        self._due = asyncio.Queue()

    async def wait(self) -> None:
        await self._due.get()

    async def fire(self) -> None:
        self._due.put_nowait(None)
        for _ in range(5):
            await asyncio.sleep(0)


def lease(redis, time: Time, renewals: Renewals | None = None) -> TelegramIdentityLease:
    return TelegramIdentityLease(
        redis,
        ACCOUNT,
        wall=time.wall,
        sleep=time.sleep,
        renew_wait=(renewals or Renewals()).wait,
        now=time.now,
    )


@pytest.fixture
def redis():
    return FakeRedis()


async def test_one_holder_at_a_time_and_the_second_waits_until_release(redis):
    time = Time()
    first, second = lease(redis, time), lease(redis, time)
    order = []

    async with first.hold(QA, wait_seconds=60, poll_seconds=5):

        async def buyer_turn():
            async with second.hold(BUYER, wait_seconds=3600, poll_seconds=5):
                order.append(("buyer", time.seconds))

        waiting = asyncio.create_task(buyer_turn())
        for _ in range(5):
            await asyncio.sleep(0)
        assert order == []
        record = await first.holder()
        assert (record["kind"], record["reference"]) == ("native_qa", "qa-run-1")
        order.append(("qa released", time.seconds))
    await waiting

    assert [event for event, _ in order] == ["qa released", "buyer"]
    assert await first.holder() is None


async def test_a_holder_that_never_releases_is_a_bounded_visible_refusal(redis):
    time = Time()
    async with lease(redis, time).hold(QA, wait_seconds=60, poll_seconds=5):
        with pytest.raises(IdentityBusy) as busy:
            async with lease(redis, time).hold(BUYER, wait_seconds=30, poll_seconds=5):
                pytest.fail("admitted beside a holder")

    assert busy.value.waited >= 30
    assert busy.value.record["reference"] == "qa-run-1"
    assert "held by native_qa qa-run-1 for exploratory" in str(busy.value)


async def test_simultaneous_admissions_admit_exactly_one(redis):
    time = Time()
    admitted = []

    async def take(holder):
        try:
            async with lease(redis, time).hold(holder, wait_seconds=0, poll_seconds=1):
                admitted.append(holder.kind)
                await asyncio.sleep(0)
        except IdentityBusy:
            admitted.append("refused")

    await asyncio.gather(take(QA), take(BUYER))

    assert len(admitted) == 2
    assert admitted.count("refused") == 1


async def test_the_holder_ends_its_use_before_another_is_admitted_even_on_error(redis):
    time = Time()
    events = []

    class Client:
        async def disconnect(self):
            events.append("disconnected")

    with pytest.raises(RuntimeError):
        async with lease(redis, time).hold(BUYER, wait_seconds=0, poll_seconds=1):
            client = Client()
            try:
                raise RuntimeError("the conversation broke")
            finally:
                await client.disconnect()
                events.append(("free?", await lease(redis, time).holder() is None))

    assert events == ["disconnected", ("free?", False)]
    assert await lease(redis, time).holder() is None


async def test_a_cancelled_holder_still_ends_its_use_before_releasing(redis):
    time = Time()
    events = []
    started = asyncio.Event()

    async def held_use():
        async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                events.append(("sandbox removed", await lease(redis, time).holder() is not None))

    task = asyncio.create_task(held_use())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [("sandbox removed", True)]
    assert await lease(redis, time).holder() is None


async def test_use_that_cannot_be_shown_ended_retains_the_identity(redis):
    time = Time()
    async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1) as held:
        held.retain("a sandbox served the QA Telegram session was not confirmed removed")
        token = held.token
    time.seconds += 3600

    record = await lease(redis, time).holder()
    assert record["retained"].startswith("a sandbox served")
    with pytest.raises(IdentityBusy) as busy:
        async with lease(redis, time).hold(BUYER, wait_seconds=10, poll_seconds=5):
            pytest.fail("admitted beside a retained hold")
    assert "retained because a sandbox served" in str(busy.value)
    assert token in str(busy.value)

    assert not await lease(redis, time).release("not-the-token")
    assert await lease(redis, time).release(token)
    async with lease(redis, time).hold(BUYER, wait_seconds=0, poll_seconds=1):
        pass


async def test_an_unrenewed_hold_is_reported_stale_and_still_never_expires(redis):
    time = Time()
    orphan = lease(redis, time)
    await orphan._acquire(QA, 0, 0)  # noqa: SLF001 - a process killed while holding
    time.seconds += 7200

    with pytest.raises(IdentityBusy) as busy:
        async with lease(redis, time).hold(BUYER, wait_seconds=10, poll_seconds=5):
            pytest.fail("admitted beside an orphan")

    assert "not renewed for" in str(busy.value)
    assert "the holder may be gone" in str(busy.value)
    assert await redis.ttl(orphan.key) == -1


async def test_ownership_lost_while_in_use_stops_the_use_and_says_so(redis):
    time = Time()
    renewals = Renewals()
    events = []
    in_use = asyncio.Event()

    async def held_use():
        async with lease(redis, time, renewals).hold(QA, wait_seconds=0, poll_seconds=1):
            try:
                in_use.set()
                await asyncio.Event().wait()
                events.append("kept using")
            finally:
                events.append("use stopped")

    task = asyncio.create_task(held_use())
    await in_use.wait()
    record = await lease(redis, time).holder()
    await lease(redis, time).release(record["token"])
    await lease(redis, time)._acquire(BUYER, 0, 0)  # noqa: SLF001 - the next holder
    await renewals.fire()

    with pytest.raises(IdentityOwnershipLost):
        await task
    assert events == ["use stopped"]
    assert (await lease(redis, time).holder())["kind"] == "synthetic_buyer"


async def test_a_renewal_refreshes_only_the_holders_own_record(redis):
    time = Time()
    renewals = Renewals()
    async with lease(redis, time, renewals).hold(QA, wait_seconds=0, poll_seconds=1):
        acquired = (await lease(redis, time).holder())["renewed_at"]
        time.seconds += 30
        await renewals.fire()
        assert (await lease(redis, time).holder())["renewed_at"] > acquired
