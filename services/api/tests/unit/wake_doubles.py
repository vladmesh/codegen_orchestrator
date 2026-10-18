"""The initialized Redis client's native Pub/Sub publish, journalled for the api wake tests."""

import asyncio

from shared.contracts.queues.scheduler_wake import SCHEDULER_WAKE_CHANNEL, parse_scheduler_wake_hint


class NativeRedis:
    """The initialized client's native Pub/Sub publish."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.receivers = 1
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: BaseException | None = None

    async def publish(self, channel: str, message: str) -> int:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        assert channel == SCHEDULER_WAKE_CHANNEL
        self.journal.append(("wake", parse_scheduler_wake_hint(message).target))
        return self.receivers
