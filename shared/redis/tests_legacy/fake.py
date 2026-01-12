from collections.abc import AsyncIterator
import json
from typing import Any

from shared.contracts.base import BaseMessage
from shared.redis.client import StreamMessage


class FakeRedisStreamClient:
    """In-memory Redis client for testing."""

    def __init__(self, redis_url: str | None = None):
        self.streams: dict[str, list[dict]] = {}
        self.connected = False
        self.groups: dict[str, set[str]] = {}  # stream -> groups

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        if stream not in self.streams:
            self.streams[stream] = []

        msg_id = f"{len(self.streams[stream]) + 1}-0"
        self.streams[stream].append({"id": msg_id, "data": json.dumps(data)})
        return msg_id

    async def publish_message(self, stream: str, message: BaseMessage) -> str:
        data = message.model_dump(mode="json")
        return await self.publish(stream, data)

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        if stream not in self.groups:
            self.groups[stream] = set()
        self.groups[stream].add(group)

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
        count: int = 1,
    ) -> AsyncIterator[StreamMessage | None]:
        # Simple replay for tests (not true blocking/group logic)
        if stream in self.streams:
            for msg in self.streams[stream]:
                data = json.loads(msg["data"])
                yield StreamMessage(message_id=msg["id"], data=data)

        # Simulate end/timeout
        yield None
