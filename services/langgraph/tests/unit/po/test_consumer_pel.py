"""What the PO consumer does with an entry besides processing it.

PO used to carry its own copy of the read loop: a start-up-only XAUTOCLAIM, a
sweep that stopped on the first page it could not claim from, a silent skip for
a body-less claim, and an XACK that destroyed a payload it could not validate.
Every one of those had already been fixed inside ``RedisStreamClient``; these
tests pin that PO now gets them from there.

The one thing PO does not get from the shared client is concurrent dispatch,
and that is what makes a recurring sweep delicate here: an entry can be legally
in flight for as long as the graph takes, so this process's own sweep comes back
round to it. What PO answers that with is the in-flight set, and what it is
worth is checked at the ratio of thresholds production runs at — see
``TestTheInFlightSetAtTheProductionRatio``. That set is per process: delivery of
``po:input`` between processes is at-least-once, exactly as it is for the six
other consumers on this client, and nothing here asserts otherwise. Most of the
tests below are about something else and use a fixture that zeroes the
thresholds so a sweep happens on every turn; that fixture is deliberately not
used where the threshold itself is the subject.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import types
from unittest.mock import AsyncMock

from fakeredis import aioredis
from langchain_core.messages import AIMessage
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from shared.redis.client import dlq_stream
from shared.tests.redis_pel_scan import PelEntry, RedisPelScan
from src.consumers import po as po_consumer

DLQ = dlq_stream(PO_INPUT_QUEUE)

# A second live PO, by a name of its own: what a dead process leaves in the PEL
# has to be reachable by another process, not only by this one.
RIVAL_CONSUMER = "po-worker-rival"

VALID_INPUT = {
    "type": "user_message",
    "text": "hello",
    "telegram_chat_id": "u1",
    "request_id": "r1",
}


async def _until(predicate, timeout: float = 5.0) -> bool:
    """Spin the event loop until *predicate* holds, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = predicate()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if outcome:
            return True
        await asyncio.sleep(0.01)
    return False


class _ReadGate:
    """A hold on the consumer's XREADGROUP, so a test can stage the PEL.

    Making an entry stuck *after* start-up means handing it to another consumer
    without letting PO's own ``>`` read win the race. The gate parks PO in front
    of the read while the test does that, and ``bypass`` is the untouched method
    for the test's own calls.
    """

    def __init__(self, redis):
        self.bypass = redis.xreadgroup
        self._open = asyncio.Event()
        self._open.set()
        self.reads = 0
        self.waiting = 0
        redis.xreadgroup = self

    async def __call__(self, *args, **kwargs):
        # fakeredis ignores the block timeout and answers straight away, so the
        # read loop only cedes control if something on the way does. Without
        # this the consumer task starves the test that is watching it.
        await asyncio.sleep(0)
        self.reads += 1
        if not self._open.is_set():
            self.waiting += 1
            try:
                await self._open.wait()
            finally:
                self.waiting -= 1
        return await self.bypass(*args, **kwargs)

    def hold(self) -> None:
        self._open.clear()

    def release(self) -> None:
        self._open.set()


class _PoRun:
    """The real ``run_po_consumer`` driven against fakeredis."""

    def __init__(self, redis, graph, client):
        self.redis = redis
        self.graph = graph
        self.client = client
        self.gate = _ReadGate(redis)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(po_consumer.run_po_consumer())

    async def settled(self) -> None:
        """Wait until the consumer has finished start-up and read once."""
        assert await _until(lambda: self.gate.reads >= 1), "consumer never reached its read loop"

    async def stop(self) -> None:
        self.gate.release()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def pending(self) -> int:
        summary = await self.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP)
        return summary["pending"]

    async def pending_is(self, expected: int) -> bool:
        return await self.pending() == expected

    async def dlq_entries(self) -> list:
        return await self.redis.xrange(DLQ)


@pytest_asyncio.fixture
async def fake_redis():
    redis = aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
def po_graph():
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="ok")]}
    state = AsyncMock()
    state.values = {"messages": []}
    graph.aget_state.return_value = state
    return graph


@pytest_asyncio.fixture
async def po_run(fake_redis, po_graph, monkeypatch):
    """Wire ``run_po_consumer`` to fakeredis and a stub graph, then hand it over.

    Only the things outside the consumer are replaced — the settings, the graph
    and the API client. The read loop, the sweep and the ack path under test are
    the production ones.
    """
    client = RedisStreamClient(redis_url="redis://fake:6379")
    client._redis = fake_redis

    async def _connect():
        client._redis = fake_redis

    async def _close():
        return None

    monkeypatch.setattr(client, "connect", _connect)
    monkeypatch.setattr(client, "close", _close)
    monkeypatch.setattr(po_consumer, "RedisStreamClient", lambda **_kwargs: client)
    monkeypatch.setattr(po_consumer, "create_po_graph", AsyncMock(return_value=po_graph))
    monkeypatch.setattr(po_consumer, "init_po_clients", lambda *_a, **_kw: None)
    monkeypatch.setattr(po_consumer.api_client, "close", AsyncMock())
    monkeypatch.setattr(
        po_consumer,
        "get_settings",
        lambda: types.SimpleNamespace(
            redis_url="redis://fake:6379",
            api_base_url="http://127.0.0.1:9",
            po_llm_model="stub",
            po_llm_base_url="",
            po_llm_api_key="",
            checkpoint_database_url="",
            summarization_model="",
            summarization_max_tokens=1,
            summarization_trigger_tokens=1,
            summarization_max_summary_tokens=1,
        ),
    )

    class _NoConfigStore:
        def __init__(self, *_a, **_kw):
            raise RuntimeError("no config store in unit tests")

    monkeypatch.setattr("shared.config_store.ConfigStore", _NoConfigStore)

    # A sweep every turn, claiming anything nobody is holding right now, so a
    # test does not have to wait out the production timeout.
    monkeypatch.setattr(po_consumer, "PEL_TIMEOUT_MS", 0)
    monkeypatch.setattr(po_consumer, "RECLAIM_INTERVAL_MS", 0, raising=False)
    monkeypatch.setattr(po_consumer, "READ_BLOCK_MS", 10, raising=False)

    run = _PoRun(fake_redis, po_graph, client)
    yield run
    await run.stop()


class TestReclaimRunsForTheProcessLifetime:
    """AC1: the sweep used to run once, before ``while True``."""

    async def test_an_entry_stuck_after_startup_is_reclaimed_without_a_restart(self, po_run):
        await po_run.start()
        await po_run.settled()

        # Park PO in front of its read, so the entry below is delivered to the
        # consumer that then dies with it, not to PO.
        po_run.gate.hold()
        assert await _until(lambda: po_run.gate.waiting >= 1)
        await po_run.redis.xadd(PO_INPUT_QUEUE, VALID_INPUT)
        await po_run.gate.bypass(
            PO_CONSUMER_GROUP, "po-worker-dead", {PO_INPUT_QUEUE: ">"}, count=1
        )
        assert await po_run.pending() == 1
        po_run.gate.release()

        assert await _until(lambda: po_run.graph.ainvoke.called), (
            "a live PO never reclaimed an entry that got stuck after start-up"
        )
        assert await _until(lambda: po_run.pending_is(0))


class TestSweepWalksThePelToItsEnd:
    """AC2: a page Redis could not claim from must not end the scan."""

    async def test_a_stale_tail_behind_a_fresh_prefix_is_reclaimed(self, po_run, monkeypatch):
        monkeypatch.setattr(po_consumer, "PEL_TIMEOUT_MS", 1_000)
        monkeypatch.setattr(po_consumer, "RECLAIM_INTERVAL_MS", 10, raising=False)

        # XAUTOCLAIM gives up after roughly ``COUNT * 10`` PEL entries, so a
        # prefix that long of entries a healthy consumer still holds answers
        # with an advanced cursor, nothing claimed and nothing deleted.
        prefix = po_consumer.READ_COUNT * 10
        fresh = [PelEntry(f"{1000 + i}-0", idle_ms=0) for i in range(prefix)]
        stale_id = f"{1000 + prefix}-0"
        stale = PelEntry(stale_id, idle_ms=60_000, fields=dict(VALID_INPUT))
        pel = RedisPelScan([*fresh, stale])
        po_run.redis.xautoclaim = pel

        await po_run.start()

        assert await _until(lambda: po_run.graph.ainvoke.called), (
            "the sweep stopped on the fresh prefix instead of following the cursor"
        )
        assert pel.start_ids[:2] == ["0-0", stale_id], (
            "the second call has to resume from the cursor Redis handed back"
        )


class TestTrimmedEntryIsDiagnosed:
    """AC4: a body-less claim used to be a silent ``continue``."""

    @pytest.mark.parametrize(
        ("answer", "lost_id"),
        [
            # Redis 6.2 leaves the body-less entry in the claimed list.
            (("0-0", [("5-5", None)]), "5-5"),
            # Redis 7 reports the ids it dropped from the PEL separately.
            (("0-0", [], ["7-7"]), "7-7"),
        ],
        ids=["redis-6.2-shape", "redis-7-shape"],
    )
    async def test_a_trimmed_pending_entry_is_logged_and_counted(self, po_run, answer, lost_id):
        po_run.redis.xautoclaim = AsyncMock(return_value=answer)

        with capture_logs() as logs:
            await po_run.start()
            assert await _until(
                lambda: po_run.client.lost_entry_count(PO_INPUT_QUEUE, PO_CONSUMER_GROUP)
            ), "the trimmed entry was skipped without being counted"

        lost = [entry for entry in logs if entry["event"] == "stream_entry_lost_to_trim"]
        assert lost, "the trimmed entry was skipped without being logged"
        assert lost[0]["entry_id"] == lost_id
        assert lost[0]["stream"] == PO_INPUT_QUEUE
        assert lost[0]["group"] == PO_CONSUMER_GROUP


class TestPoisonEntryIsObservedNotDestroyed:
    """AC5: an unvalidatable body used to be ACKed away with nothing kept."""

    async def test_a_poison_entry_reaches_the_dlq_before_it_is_acked(self, po_run):
        sentinel = "unique-context-ghp_secret_token"
        poison = {
            "type": "not_a_po_message_kind",
            "text": sentinel,
            "telegram_chat_id": "u1",
            "request_id": sentinel,
        }
        await po_run.redis.xadd(PO_INPUT_QUEUE, poison)

        with capture_logs() as logs:
            await po_run.start()
            assert await _until(lambda: po_run.dlq_entries()), "the poison entry was destroyed"

        entries = await po_run.dlq_entries()
        assert len(entries) == 1
        fields = entries[0][1]
        assert fields["source_stream"] == PO_INPUT_QUEUE
        assert fields["group"] == PO_CONSUMER_GROUP
        assert json.loads(fields["body"]) == poison

        assert await _until(lambda: po_run.pending_is(0)), "the quarantined entry was never acked"
        po_run.graph.ainvoke.assert_not_called()

        # The body is kept whole in the DLQ and out of the logs — the trust
        # boundary described in docs/ERROR_HANDLING.md, unchanged by this move.
        blob = json.dumps(logs, default=str)
        assert sentinel not in blob
        assert "input_value" not in blob

    async def test_an_entry_that_could_not_be_quarantined_is_not_acked(self, po_run):
        real_xadd = po_run.redis.xadd

        async def refuse_dlq(stream, *args, **kwargs):
            if stream == DLQ:
                raise ConnectionError("dlq unavailable")
            return await real_xadd(stream, *args, **kwargs)

        po_run.redis.xadd = refuse_dlq
        await real_xadd(PO_INPUT_QUEUE, {"type": "not_a_po_message_kind", "text": "x"})

        await po_run.start()
        assert await _until(lambda: po_run.gate.reads >= 2)
        # It is still ours to retry: nothing was destroyed because the
        # diagnostics copy failed.
        assert await po_run.pending() == 1
        assert await po_run.dlq_entries() == []

    async def test_a_message_addressed_by_the_removed_user_id_still_alerts(
        self, po_run, monkeypatch
    ):
        alert = AsyncMock()
        monkeypatch.setattr("shared.notifications.notify_admins_best_effort", alert)
        await po_run.redis.xadd(
            PO_INPUT_QUEUE,
            {**VALID_INPUT, "user_id": "42", "story_id": "story-9"},
        )

        await po_run.start()
        assert await _until(lambda: alert.await_count > 0), (
            "a message addressed by the removed user_id field passed without an alert"
        )
        message = alert.await_args.args[0]
        assert "user_id" in message
        assert await _until(lambda: po_run.dlq_entries())


class TestNewerPublisherIsNotDestroyed:
    """AC6: an added field used to fail validation and destroy the message."""

    async def test_an_unknown_field_is_dropped_and_the_message_is_handled(self, po_run):
        await po_run.redis.xadd(
            PO_INPUT_QUEUE, {**VALID_INPUT, "shiny_new_field": "from a newer publisher"}
        )

        await po_run.start()
        assert await _until(lambda: po_run.graph.ainvoke.called), (
            "a message carrying one unknown field was not handled"
        )
        assert await _until(lambda: po_run.pending_is(0))
        assert await po_run.dlq_entries() == []
        response = await po_run.redis.xrange("po:response:r1")
        assert response and response[0][1]["text"] == "ok"


@pytest.fixture
def held(monkeypatch):
    """Replace processing with something that never finishes.

    Stands in for the real thing PO does: an ``asyncio.Task`` that is still
    running, so its entry is legitimately pending and nothing acks it.
    """
    calls: list[str] = []
    release = asyncio.Event()

    async def blocking_process(_graph, _client, _sem, _locks, msg_id, _message):
        calls.append(msg_id)
        await release.wait()

    monkeypatch.setattr(po_consumer, "_process_message", blocking_process)
    yield types.SimpleNamespace(calls=calls, release=release)
    release.set()


class TestTheInFlightSetAtTheProductionRatio:
    """AC3: what keeps this process from doubling its own work.

    PO dispatches through ``asyncio.create_task`` and acks in the task's
    ``finally``, so an entry is legitimately pending for as long as the graph
    runs. Its own sweep therefore finds it idle past the bar and hands it back.
    ``in_flight`` is what stops that from becoming a second
    ``_process_message``.

    Everywhere else in this file the fixture zeroes ``PEL_TIMEOUT_MS`` so a
    sweep can claim on every turn and a test does not have to wait. That is the
    wrong setting here: the whole question is what happens at the bar a sweep
    measures idle time against, so the bar has to be real. Only the scale comes
    down — the sweep period stays half the timeout, as it is in the module.

    Nothing in this class claims mutual exclusion between processes. There is
    none: ``po:input`` is at-least-once between processes like every other
    stream on this client.
    """

    TIMEOUT_MS = 200
    SWEEP_MS = TIMEOUT_MS // 2

    @pytest.fixture
    def production_ratio(self, monkeypatch):
        monkeypatch.setattr(po_consumer, "PEL_TIMEOUT_MS", self.TIMEOUT_MS)
        monkeypatch.setattr(po_consumer, "RECLAIM_INTERVAL_MS", self.SWEEP_MS)

    async def test_our_own_sweep_does_not_dispatch_work_that_is_still_running(
        self, po_run, held, production_ratio
    ):
        reclaimed: list[str] = []
        real_xautoclaim = po_run.redis.xautoclaim

        async def recording_xautoclaim(*args, **kwargs):
            result = await real_xautoclaim(*args, **kwargs)
            reclaimed.extend(entry_id for entry_id, _fields in result[1])
            return result

        po_run.redis.xautoclaim = recording_xautoclaim

        await po_run.redis.xadd(PO_INPUT_QUEUE, VALID_INPUT)
        await po_run.start()

        assert await _until(lambda: len(held.calls) == 1)
        entry_id = held.calls[0]

        # Let the entry go idle past the bar and be handed back, more than
        # once, while the task that owns it is still running.
        assert await _until(lambda: reclaimed.count(entry_id) >= 2, timeout=5.0), (
            "the sweep never came back round to the entry this process is working on"
        )
        assert held.calls == [entry_id], (
            "a redelivery started a second _process_message for work already running"
        )

    async def test_the_id_is_released_once_the_task_has_acked(self, po_run, production_ratio):
        """A finished task must leave nothing behind that blocks the id.

        The ACK here returns without removing the entry from the PEL, so the
        sweep brings the same id back after one timeout. Only an id that has
        left the in-flight set can be dispatched again, which is what the second
        invocation proves.
        """
        acked: list[str] = []

        async def ack_without_removing(_stream, _group, msg_id):
            acked.append(msg_id)
            return 0

        po_run.redis.xack = ack_without_removing
        await po_run.redis.xadd(PO_INPUT_QUEUE, VALID_INPUT)
        await po_run.start()

        assert await _until(lambda: po_run.graph.ainvoke.await_count >= 1)
        assert await _until(lambda: po_run.graph.ainvoke.await_count >= 2, timeout=5.0), (
            "the id was never released after its task ended, so the redelivery was ignored"
        )
        assert len(acked) >= 2

    async def test_the_id_is_released_when_the_ack_fails(self, po_run, production_ratio):
        """The ACK lives in the task's ``finally``; when it raises, the entry is
        still pending but nothing is working on it any more, so the id has to go
        too — otherwise the redelivery would be refused for the life of the
        process and nobody would ever pick the entry up.
        """

        async def refuse_ack(*_args, **_kwargs):
            raise ConnectionError("ack refused")

        po_run.redis.xack = refuse_ack
        await po_run.redis.xadd(PO_INPUT_QUEUE, VALID_INPUT)
        await po_run.start()

        assert await _until(lambda: po_run.graph.ainvoke.await_count >= 1)
        assert await po_run.pending() == 1

        assert await _until(lambda: po_run.graph.ainvoke.await_count >= 2, timeout=5.0), (
            "an entry whose ACK failed was held out of reach by an id nothing was working on"
        )

    async def test_an_entry_in_flight_when_the_process_dies_lapses_after_one_timeout(
        self, po_run, held, production_ratio
    ):
        """Nothing sticky is left in Redis by a process that died: one timeout
        after it stopped, its in-flight entry is claimable by another PO.
        """
        await po_run.redis.xadd(PO_INPUT_QUEUE, VALID_INPUT)
        await po_run.start()
        assert await _until(lambda: len(held.calls) == 1)
        entry_id = held.calls[0]

        owners = await po_run.redis.xpending_range(
            PO_INPUT_QUEUE, PO_CONSUMER_GROUP, min="-", max="+", count=10
        )
        assert [owner["consumer"] for owner in owners] == [po_consumer.CONSUMER_NAME]

        await po_run.stop()
        await asyncio.sleep(self.TIMEOUT_MS * 1.5 / 1000)

        _cursor, claimed, *_ = await po_run.redis.xautoclaim(
            PO_INPUT_QUEUE,
            PO_CONSUMER_GROUP,
            RIVAL_CONSUMER,
            min_idle_time=self.TIMEOUT_MS,
            start_id="0-0",
            count=10,
        )
        assert [claimed_id for claimed_id, _fields in claimed] == [entry_id], (
            "an entry left in flight by a dead process never became claimable"
        )


class TestTheConsumerNameIdentifiesTheProcess:
    """Two live PO processes must not answer to one name.

    A consumer name is what the group keys a PEL by, so two processes sharing
    one would read each other's in-flight entries as their own, and neither the
    PEL nor a log would tell the two apart.
    """

    def test_the_name_is_not_the_pid_alone(self):
        assert po_consumer.CONSUMER_NAME.startswith("po-worker-")
        assert po_consumer.CONSUMER_NAME != f"po-worker-{os.getpid()}", (
            "two standard containers are both PID 1 and would share this name"
        )
        assert socket.gethostname() in po_consumer.CONSUMER_NAME
