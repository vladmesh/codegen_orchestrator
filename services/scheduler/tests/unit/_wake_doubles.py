"""In-memory Redis Pub/Sub, controlled timers and a cycle probe for scheduler wake tests."""

from __future__ import annotations

import asyncio

from redis.exceptions import ConnectionError as RedisConnectionError


async def settle() -> None:
    """Let every ready task run until the loop is idle again."""
    for _ in range(50):
        await asyncio.sleep(0)


class Broker:
    """Channel fan-out shared by every `StreamClient` a test hands out."""

    def __init__(self) -> None:
        self.subscriptions: list[PubSub] = []
        self.connect_failures = 0
        self.clients: list[StreamClient] = []

    def client(self) -> StreamClient:
        client = StreamClient(self)
        self.clients.append(client)
        return client

    def deliver(self, channel: str, data: object) -> None:
        for pubsub in self.subscriptions:
            if channel in pubsub.channels:
                pubsub.inbox.put_nowait({"type": "message", "channel": channel, "data": data})

    def drop_connections(self) -> None:
        for pubsub in self.subscriptions:
            pubsub.inbox.put_nowait(RedisConnectionError("connection reset"))

    async def subscribed(self) -> PubSub:
        for _ in range(50):
            live = [
                pubsub for pubsub in self.subscriptions if pubsub.channels and not pubsub.closed
            ]
            if live:
                return live[-1]
            await asyncio.sleep(0)
        raise AssertionError("no live subscription")


class PubSub:
    def __init__(self) -> None:
        self.channels: set[str] = set()
        self.inbox: asyncio.Queue[object] = asyncio.Queue()
        self.closed = 0

    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)
        self.inbox.put_nowait({"type": "subscribe", "channel": channel, "data": 1})

    async def listen(self):
        while True:
            item = await self.inbox.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self) -> None:
        self.closed += 1


class Redis:
    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def pubsub(self) -> PubSub:
        pubsub = PubSub()
        self._broker.subscriptions.append(pubsub)
        return pubsub

    async def publish(self, channel: str, data: str) -> int:
        receivers = [
            p for p in self._broker.subscriptions if channel in p.channels and not p.closed
        ]
        self._broker.deliver(channel, data)
        return len(receivers)


class StreamClient:
    """Stands in for `RedisStreamClient`: owns one connection and counts its closes."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._redis: Redis | None = None
        self.closed = 0

    async def connect(self) -> None:
        if self._broker.connect_failures:
            self._broker.connect_failures -= 1
            raise RedisConnectionError("redis unavailable")
        self._redis = Redis(self._broker)

    async def close(self) -> None:
        self.closed += 1
        self._redis = None

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("not connected")
        return self._redis


class Timers:
    """A `sleep` that only finishes when the test fires it, recording each requested delay."""

    def __init__(self) -> None:
        self.requested: list[float] = []
        self._pending: list[asyncio.Event] = []
        self.cancelled = 0

    async def sleep(self, seconds: float) -> None:
        self.requested.append(seconds)
        fired = asyncio.Event()
        self._pending.append(fired)
        try:
            await fired.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self._pending.remove(fired)

    @property
    def waiting(self) -> int:
        return len(self._pending)

    def fire(self) -> None:
        (pending,) = self._pending
        pending.set()


class Cycles:
    """A periodic-loop cycle that counts runs, detects overlap and can hold one run open."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.runs = 0
        self.active = 0
        self.max_active = 0
        self.fail_first = fail_first
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def __call__(self, _redis_client: object) -> dict[str, object]:
        self.runs += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.hold is not None:
                hold, self.hold = self.hold, None
                await hold.wait()
            if self.fail_first and self.runs == 1:
                raise RuntimeError("cycle failed")
            return {"run": self.runs}
        finally:
            self.active -= 1
