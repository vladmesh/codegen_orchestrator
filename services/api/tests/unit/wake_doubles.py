"""Native Pub/Sub publish and Run-lock session doubles, journalled for the api wake tests."""

import asyncio
from types import SimpleNamespace

from shared.contracts.dto.run import RunUpdate
from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from shared.models import Run
from src.routers.runs import update_run


class NativeRedis:
    """The initialized client's native Pub/Sub publish."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.receivers = 1
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: BaseException | None = None
        self.failures: dict[SchedulerWakeTarget, BaseException] = {}

    async def publish(self, channel: str, message: str) -> int:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        assert channel == SCHEDULER_WAKE_CHANNEL
        target = parse_scheduler_wake_hint(message).target
        if target in self.failures:
            raise self.failures[target]
        self.journal.append(("wake", target))
        return self.receivers


class RunSession:
    """Answers the Run lock and journals each commit and refresh that returns."""

    def __init__(self, journal: list, run: Run) -> None:
        self.journal = journal
        self.run = run
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: Exception | None = None

    async def execute(self, statement):
        run = self.run
        edges = SimpleNamespace(
            type=run.type, story_id=run.story_id, task_id=run.task_id, project_id=run.project_id
        )
        return SimpleNamespace(one_or_none=lambda: edges, scalar_one_or_none=lambda: run)

    async def scalar(self, statement):
        # A QA Run without a budget reservation, as one admitted before reservations.
        return None

    async def commit(self) -> None:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        self.journal.append(("commit",))

    async def refresh(self, row) -> None:
        self.journal.append(("refresh",))


def patch_run(session: RunSession, redis, *, internal: bool = True, **fields):
    """The real `PATCH /runs/{id}` handler on the session's Run."""
    return update_run(
        session.run.id,
        RunUpdate(**fields),
        db=session,
        x_telegram_id=None,
        _is_internal=internal,
        credentials=None,
        redis=redis,
    )
