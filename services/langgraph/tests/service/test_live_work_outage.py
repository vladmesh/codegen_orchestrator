"""Actual live-work/PEL boundary across an isolated TCP connection outage."""

import asyncio
from contextlib import suppress
import os
import time
from urllib.parse import urlparse
import uuid

import pytest
from structlog.testing import capture_logs

from shared.redis import RedisStreamClient
from src.consumers import _live_work as work


@pytest.fixture
def events():
    with capture_logs() as events:
        yield events


class CountedClient(RedisStreamClient):
    def __init__(self, url):
        super().__init__(url)
        self.acks = []

    async def ack(self, queue, group, message_id):
        self.acks.append(message_id)
        return await super().ack(queue, group, message_id)


class RedisProxy:
    """Drop only this test client's connections; the Redis service stays healthy."""

    def __init__(self, target):
        self.target = urlparse(target)
        self.failed = False
        self.rejected = asyncio.Event()
        self.tasks = set()
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self.connection, "127.0.0.1", 0)
        return f"redis://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/0"

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        upstream = None
        pumps = []
        try:
            if self.failed:
                self.rejected.set()
                return
            other, upstream = await asyncio.open_connection(self.target.hostname, self.target.port)
            self.writers.add(upstream)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pumps = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(other, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, upstream):
                if connection:
                    connection.close()
                    with suppress(ConnectionError):
                        await connection.wait_closed()
                    self.writers.discard(connection)
            self.tasks.discard(task)

    def cut(self):
        self.failed = True
        for writer in tuple(self.writers):
            writer.close()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


async def test_expired_unpruned_token_cannot_renew_or_prevent_pel_takeover(real_redis):
    from shared.redis import StreamMessage
    from src.consumers._base import _reclaimed_entry_is_live

    project = "expired-" + uuid.uuid4().hex
    queue, group = project + ":queue", "expired"
    client = RedisStreamClient(os.environ["REDIS_URL"])
    await client.connect()
    try:
        token = await work._begin_live_work(client, project)
        seconds, micros = await real_redis.time()
        await real_redis.zadd(
            work.live_work_leases_key(project), {token: seconds * 1000 + micros // 1000 - 1}
        )
        assert not await work._refresh_live_work_lease(client, project, token)
        await client.ensure_consumer_group(queue, group)
        entry = await real_redis.xadd(queue, {"project_id": project})
        await real_redis.xreadgroup(group, "dead", {queue: ">"})
        _, entries, _ = await real_redis.xautoclaim(queue, group, "new", 0, "0-0")
        assert entries[0][0] == entry
        assert not await _reclaimed_entry_is_live(
            client,
            StreamMessage(message_id=entry, data={"project_id": project}, reclaimed=True),
            "expired-test",
        )
        assert await real_redis.zcard(work.live_work_leases_key(project)) == 0
    finally:
        await real_redis.delete(queue, work.live_work_leases_key(project))
        await client.close()


async def test_running_execution_survives_full_ten_second_connection_outage(
    real_redis,
    record_property,
    events,
):
    async with asyncio.timeout(40):
        await _prove_ten_second_outage(real_redis, record_property, events)


async def _prove_ten_second_outage(real_redis, record_property, events):
    project = "outage-" + uuid.uuid4().hex
    queue, group = project + ":queue", "outage"
    proxy = RedisProxy(os.environ["REDIS_URL"])
    client = CountedClient(await proxy.start())
    observer = RedisStreamClient(os.environ["REDIS_URL"])
    task = None
    started, release = asyncio.Event(), asyncio.Event()
    calls = cancellations = 0

    async def process():
        nonlocal calls, cancellations
        calls += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            raise
        return {"status": "passed"}

    try:
        await client.connect()
        await observer.connect()
        await client.ensure_consumer_group(queue, group)
        entry = await real_redis.xadd(queue, {"project_id": project})
        await real_redis.xreadgroup(group, "owner", {queue: ">"})
        task = asyncio.create_task(
            work.execute_live_work(
                client,
                queue=queue,
                group=group,
                message_id=entry,
                project_id=project,
                process=process,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        original = (
            await real_redis.zrange(work.live_work_leases_key(project), 0, -1, withscores=True)
        )[0]
        # Cut before the production ten-second heartbeat, with recovery after it.
        await asyncio.sleep(1)
        outage_start = time.monotonic()
        proxy.cut()
        await asyncio.sleep(10)
        duration = time.monotonic() - outage_start
        assert duration >= 10
        assert any(
            e.get("event") == "live_work_redis_uncertain"
            and e.get("error_type") == "ConnectionError"
            for e in events
        )
        assert not task.done() and cancellations == 0
        from shared.redis import StreamMessage
        from src.consumers._base import _reclaimed_entry_is_live

        assert await _reclaimed_entry_is_live(
            observer,
            StreamMessage(message_id=entry, data={"project_id": project}, reclaimed=True),
            "outage-test",
        )
        assert (await real_redis.xpending(queue, group))["pending"] == 1
        proxy.failed = False
        async with asyncio.timeout(15):
            while True:
                current = await real_redis.zscore(work.live_work_leases_key(project), original[0])
                if current > original[1]:
                    break
                await asyncio.sleep(0.02)
        release.set()
        assert await asyncio.wait_for(task, 3) == {"status": "passed"}
        assert calls == 1 and cancellations == 0
        assert client.acks == [entry]
        assert (await real_redis.xpending(queue, group))["pending"] == 0
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not await real_redis.exists(work.live_work_failure_key(project))
        record_property("outage_seconds", duration)
        record_property(
            "fault_method", "isolated asyncio TCP proxy drops/rejects client connections"
        )
    finally:
        proxy.failed = False
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await observer.close()
        await proxy.close()
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )


@pytest.mark.parametrize("fault", ["removed", "teardown", "unproven", "unsettled"])
async def test_authoritative_loss_and_teardown_preserve_real_pending_settlement(
    real_redis,
    monkeypatch,
    fault,
):
    from shared.clients.github import WorkflowCancellationUnprovenError

    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.1)
    project = "fence-" + uuid.uuid4().hex
    queue, group = project + ":queue", "fence"
    client = CountedClient(os.environ["REDIS_URL"])
    started, cancelled = asyncio.Event(), asyncio.Event()
    task = None

    async def process():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if fault == "unproven":
                raise WorkflowCancellationUnprovenError("fixture stop unproven") from None
            if fault == "unsettled":
                return work.live_work_unsettled({"status": "failed"})
            raise

    try:
        await client.connect()
        await client.ensure_consumer_group(queue, group)
        entry = await real_redis.xadd(queue, {"project_id": project})
        await real_redis.xreadgroup(group, "owner", {queue: ">"})
        task = asyncio.create_task(
            work.execute_live_work(
                client,
                queue=queue,
                group=group,
                message_id=entry,
                project_id=project,
                process=process,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        if fault == "removed":
            await real_redis.delete(work.live_work_leases_key(project))
        else:
            await real_redis.set(work.live_work_cancel_key(project), "1")
        if fault == "teardown":
            assert await asyncio.wait_for(task, 2) is None
            assert client.acks == [entry]
            assert (await real_redis.xpending(queue, group))["pending"] == 0
        else:
            exception = {
                "removed": asyncio.CancelledError,
                "unproven": WorkflowCancellationUnprovenError,
                "unsettled": work.LiveWorkResultUnsettledError,
            }[fault]
            with pytest.raises(exception):
                await asyncio.wait_for(task, 2)
            assert client.acks == []
            assert (await real_redis.xpending(queue, group))["pending"] == 1
            assert await real_redis.exists(work.live_work_failure_key(project))
        assert cancelled.is_set()
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not any(t.get_name() == f"live-work-watch:{project}" for t in asyncio.all_tasks())
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )


async def test_process_completion_during_real_connection_uncertainty_waits_for_recovery(
    real_redis,
    monkeypatch,
    events,
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.05)
    project = "completion-" + uuid.uuid4().hex
    queue, group = project + ":queue", "completion"
    proxy = RedisProxy(os.environ["REDIS_URL"])
    client = CountedClient(await proxy.start())
    task = None
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        proxy.cut()
        return {"status": "passed"}

    try:
        await client.connect()
        await client.ensure_consumer_group(queue, group)
        entry = await real_redis.xadd(queue, {"project_id": project})
        await real_redis.xreadgroup(group, "owner", {queue: ">"})
        task = asyncio.create_task(
            work.execute_live_work(
                client,
                queue=queue,
                group=group,
                message_id=entry,
                project_id=project,
                process=process,
            )
        )
        async with asyncio.timeout(2):
            while not any(e.get("event") == "live_work_redis_uncertain" for e in events):
                await asyncio.sleep(0.01)
        assert not task.done() and client.acks == []
        assert (await real_redis.xpending(queue, group))["pending"] == 1
        proxy.failed = False
        assert await asyncio.wait_for(task, 2) == {"status": "passed"}
        assert calls == 1 and client.acks == [entry]
        assert (await real_redis.xpending(queue, group))["pending"] == 0
    finally:
        proxy.failed = False
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await proxy.close()
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )
