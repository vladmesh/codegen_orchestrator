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

    async def connect(self):
        await super().connect()
        command = self.redis.execute_command

        async def counted(*args, **kwargs):
            result = await command(*args, **kwargs)
            if result == 1 and (args[0] == "XACK" or (args[0] == "EVAL" and "'XACK'" in args[1])):
                self.acks.append(args[-1])
            return result

        # Instrument replies from native commands; every write still reaches Redis.
        self.redis.execute_command = counted


class RedisProxy:
    """Drop only this test client's connections; the Redis service stays healthy."""

    def __init__(self, target):
        self.target = urlparse(target)
        self.failed = False
        self.rejected = asyncio.Event()
        self.tasks = set()
        self.writers = set()
        self.commit_fault = None
        self.commit_dropped = asyncio.Event()
        self.commit_requests = 0

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
            drop_reply = False

            async def requests():
                nonlocal drop_reply
                # Native RESP2 commands are arrays of bulk strings. Read whole
                # frames so fault placement does not depend on TCP segmentation.
                while header := await reader.readline():
                    frame = bytearray(header)
                    arguments = []
                    for _ in range(int(header[1:])):
                        length = await reader.readline()
                        value = await reader.readexactly(int(length[1:]) + 2)
                        frame.extend(length + value)
                        arguments.append(value[:-2])
                    terminal = arguments[0] == b"XACK" or (
                        arguments[0] == b"EVAL" and b"'XACK'" in arguments[1]
                    )
                    if terminal:
                        self.commit_requests += 1
                        if self.commit_fault == "before":
                            self.commit_fault = None
                            self.commit_dropped.set()
                            self.cut()
                            return
                        if self.commit_fault == "after":
                            self.commit_fault = None
                            drop_reply = True
                    upstream.write(frame)
                    await upstream.drain()

            async def copy(source, destination):
                while data := await source.read(65536):
                    if drop_reply:
                        self.commit_dropped.set()
                        self.cut()
                        return
                    destination.write(data)
                    await destination.drain()

            pumps = [
                asyncio.create_task(requests()),
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
        for writer in tuple(self.writers):
            writer.close()
        async with asyncio.timeout(2):
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


@pytest.mark.parametrize("fault", ["completion_cancel", "teardown", "removed", "expired", "both"])
async def test_completed_unsettled_result_retains_pel_across_terminal_retry(
    real_redis,
    monkeypatch,
    events,
    fault,
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.2)
    project = "terminal-" + uuid.uuid4().hex
    queue, group = project + ":queue", "terminal"
    proxy = RedisProxy(os.environ["REDIS_URL"])
    client = CountedClient(await proxy.start())
    task = None
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        if fault == "completion_cancel":
            proxy.cut()
        else:
            proxy.commit_fault = "before"
        return work.live_work_unsettled({"status": "failed"})

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
        async with asyncio.timeout(3):
            while not any(e.get("event") == "live_work_redis_uncertain" for e in events):
                await asyncio.sleep(0.01)
        assert any(e.get("error_type") == "ConnectionError" for e in events)
        if fault != "completion_cancel":
            assert proxy.commit_dropped.is_set()
        assert not task.done()
        assert (await real_redis.xpending(queue, group))["pending"] == 1
        if fault in {"completion_cancel", "teardown", "both"}:
            await real_redis.set(work.live_work_cancel_key(project), "1")
        if fault in {"removed", "both"}:
            await real_redis.delete(work.live_work_leases_key(project))
        if fault == "expired":
            seconds, micros = await real_redis.time()
            leases = work.live_work_leases_key(project)
            tokens = await real_redis.zrange(leases, 0, -1)
            assert len(tokens) == 1
            await real_redis.zadd(leases, {tokens[0]: seconds * 1000 + micros // 1000 - 1})
        proxy.failed = False
        if fault == "completion_cancel":
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert (await real_redis.xpending(queue, group))["pending"] == 1
        assert isinstance(
            task.exception() if not task.cancelled() else asyncio.CancelledError(),
            (
                asyncio.CancelledError,
                work.LiveWorkOwnershipError,
                work.LiveWorkResultUnsettledError,
            ),
        )
        expected = b"lease_lost" if fault in {"removed", "expired"} else b"cancel_settlement_failed"
        assert await real_redis.get(work.live_work_failure_key(project)) == expected
        assert 0 < await real_redis.ttl(work.live_work_failure_key(project)) <= 120
        assert calls == 1 and client.acks == []
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not any(t.get_name() == f"live-work-watch:{project}" for t in asyncio.all_tasks())
    finally:
        proxy.failed = False
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await proxy.close()
        assert not proxy.tasks and not proxy.writers
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )


@pytest.mark.parametrize("reply_lost", [False, True])
@pytest.mark.parametrize("teardown", [False, True])
async def test_terminal_retry_requires_a_proven_ack_reply(
    real_redis,
    monkeypatch,
    events,
    reply_lost,
    teardown,
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.2)
    project = "ack-reply-" + uuid.uuid4().hex
    queue, group = project + ":queue", "ack-reply"
    proxy = RedisProxy(os.environ["REDIS_URL"])
    client = CountedClient(await proxy.start())
    task = None
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        proxy.commit_fault = "after" if reply_lost else "before"
        return work.live_work_settled({"status": "passed"})

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
        async with asyncio.timeout(3):
            while not any(e.get("event") == "live_work_redis_uncertain" for e in events):
                await asyncio.sleep(0.01)
        assert any(e.get("error_type") == "ConnectionError" for e in events)
        assert proxy.commit_dropped.is_set() and not task.done()
        assert (await real_redis.xpending(queue, group))["pending"] == int(not reply_lost)
        if teardown:
            await real_redis.set(work.live_work_cancel_key(project), "1")
        proxy.failed = False
        if reply_lost:
            with pytest.raises(work.LiveWorkAckUnprovenError):
                await asyncio.wait_for(task, 3)
            assert await real_redis.get(work.live_work_failure_key(project)) == b"ack_uncertain"
            assert 0 < await real_redis.ttl(work.live_work_failure_key(project)) <= 120
            assert client.acks == []
        else:
            assert await asyncio.wait_for(task, 3) == work.live_work_settled({"status": "passed"})
            assert client.acks == [entry]
            assert not await real_redis.exists(work.live_work_failure_key(project))
        assert calls == 1 and proxy.commit_requests == 2
        assert (await real_redis.xpending(queue, group))["pending"] == 0
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not any(t.get_name() == f"live-work-watch:{project}" for t in asyncio.all_tasks())
    finally:
        proxy.failed = False
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await proxy.close()
        assert not proxy.tasks and not proxy.writers
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )


@pytest.mark.parametrize("fault", ["before", "after"])
async def test_running_cancellation_with_uncertain_ack_preserves_primary_cancellation(
    real_redis,
    monkeypatch,
    events,
    fault,
):
    monkeypatch.setattr(work, "LIVE_WORK_LEASE_REFRESH_SECONDS", 0.2)
    project = "cancel-ack-" + uuid.uuid4().hex
    queue, group = project + ":queue", "cancel-ack"
    proxy = RedisProxy(os.environ["REDIS_URL"])
    client = CountedClient(await proxy.start())
    started, cancelled = asyncio.Event(), asyncio.Event()
    task = None
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

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
        proxy.commit_fault = fault
        await real_redis.set(work.live_work_cancel_key(project), "1")
        task.cancel()
        async with asyncio.timeout(3):
            while not any(e.get("event") == "live_work_redis_uncertain" for e in events):
                await asyncio.sleep(0.01)
        assert proxy.commit_dropped.is_set()
        assert any(e.get("error_type") == "ConnectionError" for e in events)
        assert (await real_redis.xpending(queue, group))["pending"] == int(fault == "before")
        proxy.failed = False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert await real_redis.get(work.live_work_failure_key(project)) == b"ack_uncertain"
        assert 0 < await real_redis.ttl(work.live_work_failure_key(project)) <= 120
        assert calls == 1 and cancelled.is_set() and client.acks == []
        assert (await real_redis.xpending(queue, group))["pending"] == int(fault == "before")
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not any(t.get_name() == f"live-work-watch:{project}" for t in asyncio.all_tasks())
    finally:
        proxy.failed = False
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await proxy.close()
        assert not proxy.tasks and not proxy.writers
        await real_redis.delete(
            queue,
            work.live_work_leases_key(project),
            work.live_work_cancel_key(project),
            work.live_work_failure_key(project),
        )


@pytest.mark.parametrize(
    "fault",
    ["removed", "teardown", "removed_teardown", "unproven", "unsettled", "unsettled_fence_removed"],
)
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
            if fault == "unsettled_fence_removed":
                await real_redis.delete(work.live_work_cancel_key(project))
            if fault in {"unsettled", "unsettled_fence_removed"}:
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
        if fault in {"removed", "removed_teardown"}:
            await real_redis.delete(work.live_work_leases_key(project))
        if fault != "removed":
            await real_redis.set(work.live_work_cancel_key(project), "1")
        if fault == "teardown":
            assert await asyncio.wait_for(task, 2) is None
            assert client.acks == [entry]
            assert (await real_redis.xpending(queue, group))["pending"] == 0
        else:
            exception = {
                "removed": asyncio.CancelledError,
                "removed_teardown": asyncio.CancelledError,
                "unproven": WorkflowCancellationUnprovenError,
                "unsettled": work.LiveWorkResultUnsettledError,
                "unsettled_fence_removed": work.LiveWorkResultUnsettledError,
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


@pytest.mark.parametrize("status", ["success", "passed", "skipped", "gave_up"])
@pytest.mark.parametrize("settled", [True, False, None])
async def test_completed_result_settlement_is_authoritative_under_native_teardown(
    real_redis,
    status,
    settled,
):
    project = "result-fence-" + uuid.uuid4().hex
    queue, group = project + ":queue", "result-fence"
    client = CountedClient(os.environ["REDIS_URL"])
    result = {"status": status}
    if settled is not None:
        result[work.LIVE_WORK_SETTLED_KEY] = settled

    async def process():
        await real_redis.set(work.live_work_cancel_key(project), "1")
        return result

    try:
        await client.connect()
        await client.ensure_consumer_group(queue, group)
        entry = await real_redis.xadd(queue, {"project_id": project})
        await real_redis.xreadgroup(group, "owner", {queue: ">"})
        execution = work.execute_live_work(
            client,
            queue=queue,
            group=group,
            message_id=entry,
            project_id=project,
            process=process,
        )
        if settled is True:
            assert await execution == result
            assert client.acks == [entry]
            assert not await real_redis.exists(work.live_work_failure_key(project))
        else:
            with pytest.raises(work.LiveWorkResultUnsettledError):
                await execution
            assert client.acks == []
            assert (
                await real_redis.get(work.live_work_failure_key(project))
                == b"cancel_settlement_failed"
            )
        assert (await real_redis.xpending(queue, group))["pending"] == int(settled is not True)
        assert not await real_redis.exists(work.live_work_leases_key(project))
        assert not any(t.get_name() == f"live-work-watch:{project}" for t in asyncio.all_tasks())
    finally:
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
