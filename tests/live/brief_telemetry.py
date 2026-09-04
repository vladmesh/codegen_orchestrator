"""Bounded, redacted progress facts for the paid Product Brief live suite."""

from __future__ import annotations

import hashlib
import json
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
MAX_ENGINEERING_ENTITIES = 8

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


def _compact_value(value: object, limit: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        for key in ("reason", "error", "message", "detail"):
            if key in value:
                value = value[key]
                break
        else:
            value = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    value = _safe_state(str(value))
    return f"{value[: limit - 1]}…" if len(value) > limit else value


def _engineering_task_line(task: dict, budget: int) -> str:
    task_id = _compact_value(task.get("id"), 24)
    status = _compact_value(task.get("status"), 14)
    prefix = f"T {task_id} {status} "
    error = _compact_value(task.get("failure_metadata"), max(4, min(48, budget - len(prefix))))
    line = f"{prefix}{error}"
    details = (
        f" i={_compact_value(task.get('current_iteration'), 5)}/"
        f"{_compact_value(task.get('max_iterations'), 5)}"
        f" e={_compact_value(task.get('last_event'), 16)}"
        f" u={_compact_value(task.get('updated_at'), 16)}"
    )
    return f"{line}{details}"[:budget]


def _engineering_run_line(run: dict, budget: int) -> str:
    run_id = _compact_value(run.get("id"), 24)
    status = _compact_value(run.get("status"), 14)
    prefix = f"R {run_id} {status} "
    error = _compact_value(run.get("error_message"), max(4, min(48, budget - len(prefix))))
    line = f"{prefix}{error}"
    details = (
        f" t={_compact_value(run.get('task_id'), 18)}"
        f" w={_compact_value((run.get('run_metadata') or {}).get('worker_id'), 16)}"
        f" c={_compact_value(run.get('created_at'), 16)}"
        f" u={_compact_value(run.get('updated_at'), 16)}"
    )
    return f"{line}{details}"[:budget]


def engineering_observation(
    ctx: dict,
    *,
    tasks: list[dict],
    runs: list[dict],
    runs_error: str | None = None,
) -> str:
    """Emit a changed engineering snapshot, or a compact unchanged heartbeat.

    The live suite polls the control plane frequently. Retaining its entire
    response would leak arbitrary error text and flood the workflow log, so
    this keeps only task/run progress facts and records them only on change.
    """
    _check_deadline(ctx, time.monotonic())
    raw = json.dumps(
        {"tasks": tasks, "runs": runs, "runs_error": runs_error},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    terminal = {"failed", "cancelled"}
    entities = [
        (task.get("status") not in terminal, _engineering_task_line, task) for task in tasks
    ] + [(run.get("status") not in terminal, _engineering_run_line, run) for run in runs]
    entities.sort(key=lambda item: (item[0], str(item[2].get("id", ""))))
    omitted = len(entities) - MAX_ENGINEERING_ENTITIES
    entities = entities[:MAX_ENGINEERING_ENTITIES]
    prefix = f"engineering={digest}"
    suffix = f" +{omitted}" if omitted else ""
    line_count = len(entities) + (1 if runs_error else 0)
    separators = max(line_count - 1, 0)
    budget = max(
        4,
        (STATE_MAX_CHARS - len(prefix) - len(suffix) - separators - 1) // max(line_count, 1),
    )
    lines = [renderer(entity, budget) for _, renderer, entity in entities]
    if runs_error:
        lines.append(_compact_value(runs_error, budget))
    observed_state = _safe_state(f"{prefix} {';'.join(lines)}{suffix}")
    if ctx.get("brief_engineering_observation_digest") != digest:
        ctx["brief_engineering_observation_digest"] = digest
        _record(ctx, "brief_engineering_transition", "engineering", observed_state)
    else:
        heartbeat(ctx, observed_state=f"engineering unchanged snapshot={digest}")
    return observed_state


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
