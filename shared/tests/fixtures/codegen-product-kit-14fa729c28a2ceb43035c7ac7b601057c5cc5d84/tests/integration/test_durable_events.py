from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from faststream.redis import RedisBroker, StreamSub
from faststream.redis.parser import BinaryMessageFormatV1
from framework.generators.event_adapter import EventAdapterGenerator
from framework.spec.loader import load_specs
import pytest
from redis.asyncio import Redis

from services.backend.src.app.timers import slot_instant, timer_fire
from services.backend.src.core.db import AsyncSessionLocal
from services.backend.src.core.idempotent_consumer import consume_once
from services.backend.src.generated.jobs_schemas import JOB_TIMERS
from shared.generated import events as generated_events
from shared.generated.events import EventEnvelope, get_broker, publish_event, publish_job_fired
from shared.generated.schemas import JobFired, Status, UserAccess

STREAM = "job_fired"
GROUP = "events:integration"
OWNED_STREAMS = (STREAM, "user_granted", "adapter.completed")
#: The fixture transport boundary. The live backend and its package timers publish and
#: consume `job_fired` in the product's Redis database; these tests publish, read, reclaim
#: and clean up only in their own logical database, so no live event can reach them.
INTEGRATION_REDIS_DB = 15
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _with_database(url: str, database: int) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{database}"))


LIVE_REDIS_URL = os.environ["REDIS_URL"]
assert int(urlsplit(LIVE_REDIS_URL).path.lstrip("/") or 0) != INTEGRATION_REDIS_DB
OWNED_REDIS_URL = _with_database(LIVE_REDIS_URL, INTEGRATION_REDIS_DB)


@pytest.fixture(autouse=True)
def owned_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the generated publishers and every reader at the owned database."""
    monkeypatch.setenv("REDIS_URL", OWNED_REDIS_URL)
    monkeypatch.setattr(generated_events, "_broker", None)
    for name in list(vars(generated_events)):
        if name.startswith("_pub_"):
            monkeypatch.setattr(generated_events, name, None)
    yield OWNED_REDIS_URL


async def _owned_redis() -> Redis:
    """The owned database, with only this module's streams removed."""
    redis = Redis.from_url(OWNED_REDIS_URL)
    await redis.delete(*OWNED_STREAMS)
    return redis


async def _publish_live_timer_event() -> UUID | None:
    """Publish what the core timer publishes for its current slot, into the live product."""
    if not JOB_TIMERS:
        return None
    job, period = sorted(JOB_TIMERS.items())[0]
    fire = timer_fire(job, slot_instant(datetime.now(UTC), period), "integration-product")
    envelope = EventEnvelope[JobFired](
        payload=JobFired(
            **fire.model_dump(mode="json", exclude={"contract_version"}),
            contract_version=1,
            accepted_at=datetime.now(UTC),
        )
    )
    broker = RedisBroker(LIVE_REDIS_URL, message_format=BinaryMessageFormatV1)
    await broker.connect()
    try:
        await broker.publisher(stream=STREAM).publish(envelope)
    finally:
        await broker.stop()
    return envelope.event_id


@asynccontextmanager
async def _live_timer_events() -> AsyncIterator[list[UUID]]:
    """Keep publishing live current-slot timer events while a test owns its transport."""
    published: list[UUID] = []

    async def publish() -> None:
        while True:
            if (event_id := await _publish_live_timer_event()) is None:
                return
            published.append(event_id)
            await asyncio.sleep(0.1)

    task = asyncio.create_task(publish())
    try:
        yield published
    finally:
        task.cancel()
        [outcome] = await asyncio.gather(task, return_exceptions=True)
    if isinstance(outcome, Exception):
        raise outcome
    assert published or not JOB_TIMERS


def _event() -> JobFired:
    return JobFired(
        contract_version=1,
        command_id="durable-command",
        name="integration",
        arguments={},
        fired_by_product="integration-product",
        fired_by_run="integration-run",
        accepted_at="2026-09-05T00:00:00Z",
    )


async def _read(
    consumer: str,
    *,
    stream: str = STREAM,
    group: str = GROUP,
    min_idle_time: int | None = None,
):
    broker = RedisBroker(OWNED_REDIS_URL)
    subscriber = broker.subscriber(
        stream=StreamSub(
            stream,
            group=group,
            consumer=consumer,
            min_idle_time=min_idle_time,
        )
    )
    await broker.start()
    message = await subscriber.get_one(timeout=5)
    assert message is not None
    return broker, message


def _load_generated_adapter(tmp_path: Path) -> Any:
    (tmp_path / "shared/spec").mkdir(parents=True)
    (tmp_path / "services/adapter_fixture/spec").mkdir(parents=True)
    (tmp_path / "services/adapter_fixture/src/generated").mkdir(parents=True)
    (tmp_path / "shared/spec/models.yaml").write_text(
        """
models:
  JobFired:
    fields:
      command_id:
        type: string
"""
    )
    (tmp_path / "services/adapter_fixture/spec/deliveries.yaml").write_text(
        """
domain: deliveries
operations:
  process_job:
    input: JobFired
    output: JobFired
    events:
      subscribe: job_fired
      publish_on_success: adapter.completed
"""
    )

    generated = EventAdapterGenerator(load_specs(tmp_path), tmp_path).generate()
    assert len(generated) == 1
    adapter_path = generated[0]

    package_name = f"_generated_adapter_{uuid4().hex}"
    package = ModuleType(package_name)
    package.__path__ = [str(adapter_path.parent)]  # type: ignore[attr-defined]
    protocols = ModuleType(f"{package_name}.protocols")
    protocols.DeliveriesControllerProtocol = object  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    sys.modules[protocols.__name__] = protocols

    spec = importlib.util.spec_from_file_location(f"{package_name}.event_adapter", adapter_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _wait_for_stream_length(redis: Redis, stream: str, length: int) -> None:
    async with asyncio.timeout(5):
        while await redis.xlen(stream) != length:
            await asyncio.sleep(0.01)


async def test_stream_backlog_recovery_and_idempotent_redelivery() -> None:
    redis = await _owned_redis()
    await redis.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)

    publisher = get_broker()
    await publisher.connect()
    try:
        async with _live_timer_events() as live:
            # The service group exists but no consumer is running when this event is published.
            down_envelope = await publish_job_fired(_event())
            live_broker, live_message = await _read("consumer-after-downtime")
            try:
                decoded = EventEnvelope[JobFired].model_validate(await live_message.decode())
                assert decoded.event_id == down_envelope.event_id
                assert decoded.occurred_at == down_envelope.occurred_at
                assert decoded.schema_version == 1
                await redis.xack(STREAM, GROUP, *live_message.raw_message["message_ids"])
            finally:
                await live_broker.stop()

            # This delivery is deliberately left pending, as if the process died mid-effect.
            crashed_envelope = await publish_job_fired(_event())
            crashed_broker, crashed_message = await _read("consumer-that-crashes")
            assert (
                EventEnvelope[JobFired].model_validate(await crashed_message.decode()).event_id
                == crashed_envelope.event_id
            )
            await crashed_broker.stop()

            await asyncio.sleep(0.01)
            reclaim_broker, reclaimed_message = await _read("replacement-consumer", min_idle_time=1)
            try:
                reclaimed = EventEnvelope[JobFired].model_validate(await reclaimed_message.decode())
                assert reclaimed.event_id == crashed_envelope.event_id
                await redis.xack(STREAM, GROUP, *reclaimed_message.raw_message["message_ids"])
            finally:
                await reclaim_broker.stop()

            duplicate_id = uuid4()
            await publish_job_fired(_event(), event_id=duplicate_id)
            await publish_job_fired(_event(), event_id=duplicate_id)
            calls = 0
            delivered: list[UUID] = []
            for delivery in range(2):
                duplicate_broker, message = await _read(f"idempotent-consumer-{delivery}")
                try:
                    envelope = EventEnvelope[JobFired].model_validate(await message.decode())
                    delivered.append(envelope.event_id)

                    async def effect() -> None:
                        nonlocal calls
                        calls += 1

                    async with AsyncSessionLocal() as session:
                        await consume_once(session, GROUP, envelope.event_id, effect)
                        await session.commit()
                    await redis.xack(STREAM, GROUP, *message.raw_message["message_ids"])
                finally:
                    await duplicate_broker.stop()

            assert delivered == [duplicate_id, duplicate_id]
            assert calls == 1
        # Live current-slot timer events were published throughout; none reached this group.
        assert not set(live) & {down_envelope.event_id, crashed_envelope.event_id, duplicate_id}
        assert await redis.xlen(STREAM) == 4
    finally:
        await publisher.stop()
        await redis.delete(*OWNED_STREAMS)
        await redis.aclose()


async def test_publish_event_uses_the_real_stream_transport() -> None:
    stream = "user_granted"
    group = "events:publish-event-integration"
    redis = await _owned_redis()
    await redis.xgroup_create(stream, group, id="0-0", mkstream=True)

    publisher = get_broker()
    await publisher.connect()
    try:
        payload = UserAccess(
            user_id=42,
            status=Status.active,
            channel="telegram",
            external_id="integration-user",
        )
        published = await publish_event(stream, payload)
        reader, message = await _read(
            "publish-event-reader",
            stream=stream,
            group=group,
        )
        try:
            decoded = EventEnvelope[UserAccess].model_validate(await message.decode())
            assert decoded == published
            await redis.xack(stream, group, *message.raw_message["message_ids"])
        finally:
            await reader.stop()
    finally:
        await publisher.stop()
        await redis.delete(*OWNED_STREAMS)
        await redis.aclose()


async def test_generated_adapter_guards_live_and_reclaimed_delivery(tmp_path: Path) -> None:
    adapter = _load_generated_adapter(tmp_path)
    redis = await _owned_redis()

    effect_started = asyncio.Event()
    allow_effect_to_finish = asyncio.Event()
    both_deliveries_guarded = asyncio.Event()

    class SlowController:
        calls = 0

        async def process_job(self, session, *, payload: JobFired) -> JobFired:
            assert session is not None
            self.calls += 1
            effect_started.set()
            await allow_effect_to_finish.wait()
            return payload

    controller = SlowController()
    guard_calls = 0
    guarded: list[UUID] = []

    async def guarded_consume_once(session, consumer_group, event_id, effect):
        nonlocal guard_calls
        guard_calls += 1
        guarded.append(event_id)
        if guard_calls == 2:
            both_deliveries_guarded.set()
        return await consume_once(session, consumer_group, event_id, effect)

    adapter_broker = RedisBroker(OWNED_REDIS_URL)
    adapter.create_event_adapter(
        adapter_broker,
        get_session=AsyncSessionLocal,
        consume_once=guarded_consume_once,
        get_deliveries_controller=lambda: controller,
        reclaim_idle_ms=100,
        reclaim_polling_interval_ms=20,
    )
    publisher = get_broker()
    await publisher.connect()
    await adapter_broker.start()
    try:
        async with _live_timer_events() as live:
            published = await publish_job_fired(_event())
            await asyncio.wait_for(effect_started.wait(), timeout=5)

            # Keep the live handler in flight beyond the reclaim window. The recovery
            # reader must enter the same guard and block behind its uncommitted row.
            await asyncio.wait_for(both_deliveries_guarded.wait(), timeout=5)
            assert controller.calls == 1

            allow_effect_to_finish.set()
            await _wait_for_stream_length(redis, "adapter.completed", 1)
            async with asyncio.timeout(5):
                while (await redis.xpending(STREAM, adapter.CONSUMER_GROUP))["pending"]:
                    await asyncio.sleep(0.01)

        assert guard_calls == 2
        assert guarded == [published.event_id, published.event_id]
        assert published.event_id not in live
        assert controller.calls == 1
        assert await redis.xlen("adapter.completed") == 1
    finally:
        allow_effect_to_finish.set()
        await adapter_broker.stop()
        await publisher.stop()
        await redis.delete(*OWNED_STREAMS)
        await redis.aclose()
