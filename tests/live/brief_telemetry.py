"""Bounded, redacted progress facts for the paid Product Brief live suite."""

from __future__ import annotations

import os
import time

import structlog

from shared.contracts.worker_evidence import secret_env_values
from shared.diagnostics import redact_diagnostic
from shared.stand_deadlines import MEGA_BRIEF_PRODUCTIVE_SECONDS

PRODUCTIVE_DEADLINE_SECONDS = MEGA_BRIEF_PRODUCTIVE_SECONDS
HEARTBEAT_SECONDS = 30
STATE_MAX_CHARS = 240
MAX_EVENTS = 128

logger = structlog.get_logger(__name__)


class ProductiveDeadlineExceeded(RuntimeError):
    """The fixture reached its productive deadline and must enter teardown."""


def begin(ctx: dict) -> None:
    started_at = time.monotonic()
    ctx["brief_productive_started_at"] = started_at
    ctx["brief_productive_deadline_at"] = started_at + PRODUCTIVE_DEADLINE_SECONDS
    ctx["brief_telemetry"] = []
    ctx["brief_next_heartbeat_at"] = started_at


def _elapsed(ctx: dict, now: float) -> int:
    return int(now - ctx["brief_productive_started_at"])


def _safe_state(value: str) -> str:
    state = redact_diagnostic(value, secrets=secret_env_values(dict(os.environ)))
    return f"{state[: STATE_MAX_CHARS - 1]}…" if len(state) > STATE_MAX_CHARS else state


def _check_deadline(ctx: dict, now: float) -> None:
    if now < ctx["brief_productive_deadline_at"]:
        return
    ctx["brief_deadline_exhausted"] = True
    ctx["brief_stopped_stage"] = ctx.get("brief_active_stage", "unknown")
    logger.warning(
        "brief_productive_deadline_exhausted",
        elapsed_seconds=_elapsed(ctx, now),
        stopped_stage=ctx["brief_stopped_stage"],
    )
    raise ProductiveDeadlineExceeded(
        f"mega-brief productive deadline of {PRODUCTIVE_DEADLINE_SECONDS}s exhausted"
    )


def _record(ctx: dict, event: str, stage: str, observed_state: str) -> None:
    now = time.monotonic()
    entry = {
        "event": event,
        "stage": stage,
        "elapsed_seconds": _elapsed(ctx, now),
        "observed_state": _safe_state(observed_state),
    }
    events = ctx["brief_telemetry"]
    if len(events) == MAX_EVENTS:
        events.pop(0)
    events.append(entry)
    logger.info(event, **{key: value for key, value in entry.items() if key != "event"})


def stage(ctx: dict, name: str, *, observed_state: str, enforce_deadline: bool = True) -> None:
    now = time.monotonic()
    if enforce_deadline:
        _check_deadline(ctx, now)
    ctx["brief_active_stage"] = name
    _record(ctx, "brief_stage", name, observed_state)
    ctx["brief_next_heartbeat_at"] = now + HEARTBEAT_SECONDS


def heartbeat(ctx: dict, *, observed_state: str) -> None:
    now = time.monotonic()
    _check_deadline(ctx, now)
    if now < ctx["brief_next_heartbeat_at"]:
        return
    _record(ctx, "brief_heartbeat", ctx.get("brief_active_stage", "unknown"), observed_state)
    ctx["brief_next_heartbeat_at"] = now + HEARTBEAT_SECONDS


def evidence(ctx: dict) -> dict | None:
    if "brief_productive_started_at" not in ctx:
        return None
    now = time.monotonic()
    return {
        "productive_deadline_seconds": PRODUCTIVE_DEADLINE_SECONDS,
        "elapsed_seconds": _elapsed(ctx, now),
        "active_stage": ctx.get("brief_active_stage", "unknown"),
        "stopped_stage": ctx.get("brief_stopped_stage"),
        "deadline_exhausted": ctx.get("brief_deadline_exhausted", False),
        "events": list(ctx["brief_telemetry"]),
    }
