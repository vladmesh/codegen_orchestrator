"""The core timer loop fires declared timer jobs through the real jobs core."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from services.backend.src.app import lifespan as lifespan_module, timers as timers_module
from services.backend.src.app.models.job_command import DispatchStatus, JobCommand
from services.backend.src.app.timers import (
    TIMER_RUN,
    FireTimer,
    TimerLoop,
    fire_through_jobs_core,
    timer_fire,
)
from services.backend.src.controllers import jobs as jobs_controller
from services.backend.src.controllers.jobs import JobsController
from services.backend.src.core.settings import get_settings
from services.backend.src.generated.jobs_schemas import JOB_SCHEMAS
from shared.generated.schemas import JobCommand as JobCommandContract

TICK = "reminders.tick"
PRODUCT = "product-a"
AT_ONLY = {
    "type": "object",
    "properties": {"at": {"type": "string", "format": "date-time"}},
    "required": ["at"],
    "additionalProperties": False,
}
SLOT = datetime(2026, 10, 2, 19, 43, tzinfo=UTC)
SECONDS_TO_NEXT_SLOT = 32.5
TWO_SLOTS = 2


class Stop(Exception):
    """Ends ``TimerLoop.run`` after a bounded number of periods."""


class Clock:
    """A controllable wall clock whose sleep advances time instead of waiting."""

    def __init__(self, now: datetime, *, periods: int = 0) -> None:
        self.now = now
        self.sleeps: list[float] = []
        self.periods = periods

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.periods:
            raise Stop
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def declared_tick() -> Generator[None, None, None]:
    JOB_SCHEMAS.clear()
    JOB_SCHEMAS.update({TICK: AT_ONLY})
    yield
    JOB_SCHEMAS.clear()


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    recorded: list[Any] = []

    async def _publish(message: Any) -> None:
        recorded.append(message)

    monkeypatch.setattr(jobs_controller, "publish_job_fired", _publish)
    return recorded


def _fire_in(session: AsyncSession) -> FireTimer:
    async def fire(job: str, slot: datetime) -> JobCommandContract:
        return await JobsController().fire(session, timer_fire(job, slot, PRODUCT))

    return fire


async def _commands(session: AsyncSession) -> list[JobCommand]:
    return list((await session.execute(select(JobCommand))).scalars().all())


@pytest.mark.asyncio
async def test_one_period_records_and_emits_one_tick_at_the_slot_instant(
    db_session: AsyncSession, emitted: list[Any]
) -> None:
    clock = Clock(SLOT + timedelta(seconds=27.5))
    loop = TimerLoop({TICK: 60}, _fire_in(db_session), clock=clock)

    wait = await loop.fire_due()
    again = await loop.fire_due()

    [command] = await _commands(db_session)
    assert command.name == TICK
    assert command.arguments == {"at": "2026-10-02T19:43:00Z"}
    assert datetime.fromisoformat(command.arguments["at"]) == SLOT
    assert command.command_id == "core-timer:reminders.tick:2026-10-02T19:43:00Z"
    assert command.fired_by_product == PRODUCT
    assert command.fired_by_run == TIMER_RUN
    assert command.dispatch_status is DispatchStatus.DISPATCHED
    assert len(emitted) == 1
    assert emitted[0].arguments == {"at": "2026-10-02T19:43:00Z"}
    assert emitted[0].fired_by_run == TIMER_RUN
    assert wait == again == SECONDS_TO_NEXT_SLOT


@pytest.mark.asyncio
async def test_refiring_a_slot_after_a_restart_records_and_emits_nothing_new(
    db_session: AsyncSession, emitted: list[Any]
) -> None:
    before_restart = TimerLoop(
        {TICK: 60}, _fire_in(db_session), clock=Clock(SLOT + timedelta(seconds=5))
    )
    await before_restart.fire_due()
    evidence: list[JobCommandContract] = []

    async def recording_fire(job: str, slot: datetime) -> JobCommandContract:
        command = await _fire_in(db_session)(job, slot)
        evidence.append(command)
        return command

    after_restart = TimerLoop({TICK: 60}, recording_fire, clock=Clock(SLOT + timedelta(seconds=50)))
    await after_restart.fire_due()

    assert len(await _commands(db_session)) == 1
    assert len(emitted) == 1
    assert [command.dispatch_status.value for command in evidence] == ["dispatched"]


@pytest.mark.asyncio
async def test_a_failed_fire_is_logged_and_the_next_slot_still_fires(
    db_session: AsyncSession, emitted: list[Any]
) -> None:
    clock = Clock(SLOT, periods=1)
    attempts: list[datetime] = []

    async def failing_once(job: str, slot: datetime) -> JobCommandContract:
        attempts.append(slot)
        if len(attempts) == 1:
            raise RuntimeError("database unavailable")
        return await _fire_in(db_session)(job, slot)

    loop = TimerLoop({TICK: 60}, failing_once, clock=clock, sleep=clock.sleep)
    with capture_logs() as logs, pytest.raises(Stop):
        await loop.run()

    assert attempts == [SLOT, SLOT + timedelta(seconds=60)]
    assert clock.sleeps == [60.0, 60.0]
    [failure] = [entry for entry in logs if entry["event"] == "core_timer_fire_failed"]
    assert failure["job"] == TICK
    assert failure["slot"] == "2026-10-02T19:43:00Z"
    assert failure["exception_type"] == "RuntimeError"
    [command] = await _commands(db_session)
    assert command.arguments == {"at": "2026-10-02T19:44:00Z"}
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_a_missed_slot_is_covered_by_the_next_slot_not_replayed(
    db_session: AsyncSession, emitted: list[Any]
) -> None:
    clock = Clock(SLOT)
    loop = TimerLoop({TICK: 60}, _fire_in(db_session), clock=clock)
    await loop.fire_due()

    clock.now = SLOT + timedelta(minutes=10, seconds=3)
    await loop.fire_due()

    assert [command.arguments["at"] for command in await _commands(db_session)] == [
        "2026-10-02T19:43:00Z",
        "2026-10-02T19:53:00Z",
    ]
    assert len(emitted) == TWO_SLOTS


@pytest.mark.asyncio
async def test_an_undelivered_fire_is_logged_and_left_for_a_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable_broker(message: Any) -> None:
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(jobs_controller, "publish_job_fired", unavailable_broker)
    loop = TimerLoop({TICK: 60}, _fire_in(db_session), clock=Clock(SLOT))

    with capture_logs() as logs:
        await loop.fire_due()

    [command] = await _commands(db_session)
    assert command.dispatch_status is DispatchStatus.UNDELIVERED
    assert [entry["event"] for entry in logs] == ["core_timer_fire_undelivered"]


@pytest.mark.asyncio
async def test_the_production_fire_uses_the_jobs_core_in_its_own_session(
    db_session: AsyncSession, emitted: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def session_local() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("services.backend.src.core.db.AsyncSessionLocal", session_local)

    command = await fire_through_jobs_core(TICK, SLOT)

    assert command.fired_by_product == get_settings().app_name
    assert command.fired_by_run == TIMER_RUN
    assert command.arguments == {"at": "2026-10-02T19:43:00Z"}
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_lifespan_starts_no_loop_without_declared_timers(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifespan_module, "JOB_TIMERS", {})

    async with app.router.lifespan_context(app):
        assert app.state.codegen_timer_loop is None


@pytest.mark.asyncio
async def test_lifespan_starts_one_loop_for_declared_timers_and_stops_it(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = asyncio.Event()
    slots: list[tuple[str, datetime]] = []

    async def fake_fire(job: str, slot: datetime) -> JobCommandContract:
        slots.append((job, slot))
        fired.set()
        raise RuntimeError("not recorded in this test")

    monkeypatch.setattr(lifespan_module, "JOB_TIMERS", {TICK: 60})
    monkeypatch.setattr(timers_module, "fire_through_jobs_core", fake_fire)

    async with app.router.lifespan_context(app):
        loop = app.state.codegen_timer_loop
        assert isinstance(loop, TimerLoop)
        assert loop.timers == {TICK: 60}
        assert loop.running is True
        await asyncio.wait_for(fired.wait(), timeout=5)

    assert [job for job, _ in slots] == [TICK]
    assert loop.running is False
