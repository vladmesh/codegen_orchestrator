"""The core timer loop: package-declared timer jobs fired through the jobs core.

Every timer in the generated ``JOB_TIMERS`` owns a fixed grid of slots, the multiples
of its period since the Unix epoch. Once per slot the loop fires the timer's job with
``{"at": <slot instant>}`` through ``JobsController.fire``, the same record-then-emit
path as ``POST /jobs/fire``; there is no second dispatch path. The command identity is
derived from the job and the slot, so a restart inside the slot, or a second backend
process firing the same slot, finds the recorded ``(fired_by_product, command_id)``
and emits nothing new.

A failed fire is logged and the loop waits for the next slot. A slot missed while the
process was down is not replayed: the next slot's later ``at`` covers it, because a
timer job's ``at`` is the instant everything up to which is evaluated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

import structlog

from shared.generated.schemas import DispatchStatus, JobCommand, JobFire

logger = structlog.stdlib.get_logger()

TIMER_RUN = "core-timer"

FireTimer = Callable[[str, datetime], Awaitable[JobCommand]]


def slot_instant(now: datetime, every_seconds: int) -> datetime:
    """Return the start of the slot of an ``every_seconds`` grid that contains ``now``."""

    seconds = int(now.timestamp())
    return datetime.fromtimestamp(seconds - seconds % every_seconds, UTC)


def _instant(slot: datetime) -> str:
    return slot.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def timer_command_id(job: str, slot: datetime) -> str:
    """The deterministic command identity of one job's one slot."""

    return f"{TIMER_RUN}:{job}:{_instant(slot)}"


def timer_fire(job: str, slot: datetime, fired_by_product: str) -> JobFire:
    """The ``JobFire`` the core timer sends for one job's one slot."""

    return JobFire(
        command_id=timer_command_id(job, slot),
        name=job,
        arguments={"at": _instant(slot)},
        fired_by_product=fired_by_product,
        fired_by_run=TIMER_RUN,
    )


async def fire_through_jobs_core(job: str, slot: datetime) -> JobCommand:
    """Fire one slot in its own session, exactly as ``POST /jobs/fire`` would."""

    from services.backend.src.controllers.jobs import JobsController
    from services.backend.src.core.db import AsyncSessionLocal
    from services.backend.src.core.settings import get_settings

    async with AsyncSessionLocal() as session:
        return await JobsController().fire(session, timer_fire(job, slot, get_settings().app_name))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TimerLoop:
    """Fire each declared timer once per slot until stopped."""

    def __init__(
        self,
        timers: Mapping[str, int],
        fire: FireTimer | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.timers = dict(sorted(timers.items()))
        self._fire = fire or fire_through_jobs_core
        self._clock = clock
        self._sleep = sleep
        self._attempted: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the loop's background task has been started and not stopped."""

        return self._task is not None

    async def fire_due(self) -> float:
        """Fire every timer whose current slot is unattempted; return seconds to the next."""

        now = self._clock()
        waits: list[float] = []
        for job, every_seconds in self.timers.items():
            slot = slot_instant(now, every_seconds)
            waits.append((slot + timedelta(seconds=every_seconds) - now).total_seconds())
            attempted = self._attempted.get(job)
            if attempted is not None and slot <= attempted:
                continue
            self._attempted[job] = slot
            await self._fire_slot(job, slot)
        return max(min(waits), 0.0)

    async def _fire_slot(self, job: str, slot: datetime) -> None:
        try:
            command = await self._fire(job, slot)
        except Exception as error:
            logger.error(
                "core_timer_fire_failed",
                job=job,
                slot=_instant(slot),
                exception_type=type(error).__name__,
                exc_info=error,
            )
            return
        if command.dispatch_status is not DispatchStatus.dispatched:
            logger.warning(
                "core_timer_fire_undelivered",
                job=job,
                slot=_instant(slot),
                command_id=command.command_id,
            )

    async def run(self) -> None:
        """Fire due slots, then sleep until the earliest next slot, until cancelled."""

        while True:
            await self._sleep(await self.fire_due())

    def start(self) -> None:
        """Start the loop as one background task of the running event loop."""

        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="core-timer-loop")

    async def stop(self) -> None:
        """Cancel the loop and wait for it, without swallowing the caller's cancellation."""

        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as error:
            # Every fire is already guarded; never let the loop turn shutdown into a failure.
            logger.error(
                "core_timer_loop_failed", exception_type=type(error).__name__, exc_info=error
            )


def start_timer_loop(timers: Mapping[str, int]) -> TimerLoop | None:
    """Start the one core timer loop, or none when no timer is declared."""

    if not timers:
        return None
    loop = TimerLoop(timers)
    loop.start()
    return loop


__all__ = [
    "TIMER_RUN",
    "TimerLoop",
    "fire_through_jobs_core",
    "slot_instant",
    "start_timer_loop",
    "timer_command_id",
    "timer_fire",
]
