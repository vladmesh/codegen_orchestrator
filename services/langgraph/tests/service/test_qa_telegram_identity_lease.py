"""The shared QA Telegram identity's hold, against a real Redis.

The unit suite drives the same adapter over an in-memory Redis. Here the Lua
scripts, the no-TTL record, the watchdog's renewal and the executor-removal answer
read off the real `worker:responses` stream run against the service leg's Redis:
admission is atomic under contention, nothing expires by time, a holder whose
record is taken stops its use, and an executor's removal counts only once
worker-manager's answer to that exact delete command arrives.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from shared.contracts.queues.worker import DeleteWorkerResponse
from shared.queues import WORKER_RESPONSES
from src.clients.qa_worker import ExecutorRemovals, _confirm_removal
from src.consumers._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityOwnershipLost,
    TelegramIdentityLease,
)


@pytest.fixture
async def account(real_redis):
    """A Telegram id of this test alone, so no other case shares its lease key."""
    telegram_id = int(uuid.uuid4().int % 10**9) + 10**9
    yield telegram_id
    await real_redis.delete(f"qa:telegram-identity:{telegram_id}")


def _holder(index: int) -> Holder:
    kind = HolderKind.NATIVE_QA if index % 2 else HolderKind.SYNTHETIC_BUYER
    return Holder(kind, f"holder-{index}", "contention")


async def test_contending_holders_are_admitted_one_at_a_time(real_redis, account):
    inside = 0
    most = 0
    admitted = []

    async def use(index: int) -> None:
        nonlocal inside, most
        lease = TelegramIdentityLease(real_redis, account)
        async with lease.hold(_holder(index), wait_seconds=30, poll_seconds=0.01):
            inside += 1
            most = max(most, inside)
            admitted.append(index)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(use(index) for index in range(8)))

    assert most == 1
    assert sorted(admitted) == list(range(8))
    assert await real_redis.exists(f"qa:telegram-identity:{account}") == 0


async def test_a_retained_hold_never_expires_and_yields_only_to_its_token(real_redis, account):
    lease = TelegramIdentityLease(real_redis, account)
    async with lease.hold(_holder(1), wait_seconds=1, poll_seconds=0.05) as held:
        held.retain("a sandbox served the QA Telegram session was not confirmed removed")

    assert await real_redis.ttl(lease.key) == -1
    with pytest.raises(IdentityBusy) as busy:
        async with lease.hold(_holder(2), wait_seconds=0.3, poll_seconds=0.05):
            pytest.fail("admitted beside a retained hold")
    assert "retained because a sandbox served" in str(busy.value)
    assert not await lease.release("another-token")
    assert await lease.release(held.token)
    async with lease.hold(_holder(2), wait_seconds=1, poll_seconds=0.05):
        pass


async def test_a_holder_whose_record_is_taken_stops_its_use(real_redis, account):
    lease = TelegramIdentityLease(real_redis, account, renew_wait=lambda: asyncio.sleep(0.05))
    stopped = asyncio.Event()
    in_use = asyncio.Event()

    async def use() -> None:
        async with lease.hold(_holder(1), wait_seconds=1, poll_seconds=0.05):
            try:
                in_use.set()
                await asyncio.sleep(30)
            finally:
                stopped.set()

    task = asyncio.create_task(use())
    await in_use.wait()
    await real_redis.delete(lease.key)

    with pytest.raises(IdentityOwnershipLost):
        await asyncio.wait_for(task, timeout=5)
    assert stopped.is_set()


async def test_an_executor_removal_counts_on_worker_managers_answer_alone(real_redis):
    group = f"qa-client-{uuid.uuid4().hex[:8]}"
    await real_redis.xgroup_create(WORKER_RESPONSES, group, id="$", mkstream=True)
    request = uuid.uuid4().hex
    removals = ExecutorRemovals()
    try:
        confirming = asyncio.create_task(
            _confirm_removal(real_redis, group, "qa-test", request, "qa-worker-1", removals)
        )
        # Another request's answer first: it confirms nothing about this executor.
        for request_id in (f"cleanup-{uuid.uuid4().hex}", f"cleanup-{request}"):
            answer = DeleteWorkerResponse(request_id=request_id, success=True)
            await real_redis.xadd(
                WORKER_RESPONSES, {"data": json.dumps(answer.model_dump(mode="json"))}
            )
        await asyncio.wait_for(confirming, timeout=10)
    finally:
        await real_redis.xgroup_destroy(WORKER_RESPONSES, group)

    assert removals.confirmed == ["qa-worker-1"]
    assert removals.unconfirmed == {}
