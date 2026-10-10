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
async def redis():
    """A Redis whose identity record an operator has initialized to idle."""
    redis = FakeRedis()
    assert await lease(redis, Time()).initialize()
    return redis


async def test_one_holder_at_a_time_and_the_second_waits_until_release(redis):
    time = Time()
    first, second = lease(redis, time), lease(redis, time)
    order = []

    async with first.hold(QA, wait_seconds=60, poll_seconds=5):

        async def buyer_turn():
            async with second.hold(BUYER, wait_seconds=3600, poll_seconds=5):
                order.append("buyer")

        waiting = asyncio.create_task(buyer_turn())
        for _ in range(5):
            await asyncio.sleep(0)
        assert order == []
        record = await first.holder()
        assert (record["state"], record["kind"], record["reference"]) == (
            "held",
            "native_qa",
            "qa-run-1",
        )
        order.append("qa released")
    await waiting

    assert order == ["qa released", "buyer"]
    record = await first.holder()
    assert record["state"] == "idle"
    assert "token" not in record


async def test_a_missing_record_admits_no_one_and_is_never_created_by_an_acquire():
    redis, time = FakeRedis(), Time()

    with pytest.raises(IdentityBusy) as busy:
        async with lease(redis, time).hold(QA, wait_seconds=30, poll_seconds=5):
            pytest.fail("admitted with no known admission state")

    assert "missing or unknown" in str(busy.value)
    assert "identity --initialize" in str(busy.value)
    assert await redis.exists(lease(redis, time).key) == 0


@pytest.mark.parametrize(
    "record",
    [
        {"state": "free"},
        {"state": "held"},  # held without the token that names its holder
        {"token": "t", "kind": "native_qa"},
    ],
)
async def test_a_malformed_record_admits_no_one_until_initialized(record):
    redis, time = FakeRedis(), Time()
    await redis.hset(lease(redis, time).key, mapping=record)

    with pytest.raises(IdentityBusy, match="missing or unknown"):
        async with lease(redis, time).hold(QA, wait_seconds=10, poll_seconds=5):
            pytest.fail("admitted on a malformed record")

    assert await lease(redis, time).initialize()
    async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1):
        pass


async def test_initialize_never_overwrites_a_known_state(redis):
    time = Time()
    assert not await lease(redis, time).initialize()  # idle already
    async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1):
        assert not await lease(redis, time).initialize()  # held
        assert (await lease(redis, time).holder())["state"] == "held"


async def test_a_lost_record_admits_no_second_user_while_the_first_is_still_connected(redis):
    """Loss while A is in use and B applies before any watchdog or cleanup runs."""
    time = Time()
    renewals = Renewals()
    in_use = asyncio.Event()
    connected = []

    async def holder_a():
        async with lease(redis, time, renewals).hold(QA, wait_seconds=0, poll_seconds=1) as held:
            client = held.track("client", "A")
            connected.append("A")
            try:
                in_use.set()
                await asyncio.Event().wait()
            finally:
                connected.remove("A")
                client.end()

    task = asyncio.create_task(holder_a())
    await in_use.wait()
    await redis.delete(lease(redis, time).key)

    with pytest.raises(IdentityBusy, match="missing or unknown"):
        async with lease(redis, time).hold(BUYER, wait_seconds=30, poll_seconds=5):
            connected.append("B")
    assert connected == ["A"]

    await renewals.fire()
    with pytest.raises(IdentityOwnershipLost):
        await task
    assert connected == []
    # The late release of A did not turn the lost record into an idle one.
    assert await redis.exists(lease(redis, time).key) == 0


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


async def test_every_lifetime_ended_on_proof_releases_on_error(redis):
    time = Time()
    events = []

    with pytest.raises(RuntimeError):
        async with lease(redis, time).hold(BUYER, wait_seconds=0, poll_seconds=1) as held:
            client = held.track("client", "buyer session")
            try:
                raise RuntimeError("the conversation broke")
            finally:
                events.append(("held while ending", (await lease(redis, time).holder())["state"]))
                client.end()

    assert events == [("held while ending", "held")]
    assert (await lease(redis, time).holder())["state"] == "idle"


@pytest.mark.parametrize("where", ["disconnect", "probe", "endpoint stop"])
async def test_a_use_cancelled_before_its_end_was_proven_retains_the_identity(redis, where):
    time = Time()
    started = asyncio.Event()

    async def held_use():
        async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1) as held:
            used = held.track("client" if where == "disconnect" else where, where)
            started.set()
            await asyncio.Event().wait()  # the disconnect / probe exit / stop being awaited
            used.end()

    task = asyncio.create_task(held_use())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = await lease(redis, time).holder()
    assert record["state"] == "retained"
    assert where in record["retained"]
    with pytest.raises(IdentityBusy, match="retained because outstanding"):
        async with lease(redis, time).hold(BUYER, wait_seconds=5, poll_seconds=5):
            pytest.fail("admitted past a use that was not shown ended")


async def test_an_unproven_lifetime_retains_and_yields_only_to_its_token(redis):
    time = Time()
    async with lease(redis, time).hold(QA, wait_seconds=0, poll_seconds=1) as held:
        sandbox = held.track("sandbox", "qa-abc")
        sandbox.unproven("worker-manager did not prove the removal")
        token = held.token
    time.seconds += 3600

    record = await lease(redis, time).holder()
    assert record["state"] == "retained"
    assert record["retained"] == (
        "outstanding: sandbox qa-abc (worker-manager did not prove the removal)"
    )
    with pytest.raises(IdentityBusy) as busy:
        async with lease(redis, time).hold(BUYER, wait_seconds=10, poll_seconds=5):
            pytest.fail("admitted beside a retained hold")
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
    assert await lease(redis, time).release(record["token"])  # an operator's release
    await renewals.fire()

    with pytest.raises(IdentityOwnershipLost):
        await task
    assert events == ["use stopped"]
    assert (await lease(redis, time).holder())["state"] == "idle"


async def test_a_renewal_refreshes_only_the_holders_own_record(redis):
    time = Time()
    renewals = Renewals()
    async with lease(redis, time, renewals).hold(QA, wait_seconds=0, poll_seconds=1):
        acquired = (await lease(redis, time).holder())["renewed_at"]
        time.seconds += 30
        await renewals.fire()
        assert (await lease(redis, time).holder())["renewed_at"] > acquired
