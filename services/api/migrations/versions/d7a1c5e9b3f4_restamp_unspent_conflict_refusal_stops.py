"""Restamp conflict repair stops that a paid refusal mislabelled as exhausted.

Before this revision a no-Run paid refusal of a PR-conflict repair Task stopped
its Story with `pr_conflict_repair_exhausted`, although no repair Run ever
existed. The repair command now re-admits such a Task only while the Story
carries the refusal's own stop. This data migration finds the old shape, proven
by immutable Task, event, paid-audit and Run rows, and restamps only the stop's
code (`engineering_budget_denied` / `engineering_dispatch_refused`). Nothing is
dispatched; the ordinary repair command stays the deliberate next step. Every
other Story is left alone, and a second run finds nothing.

Revision ID: d7a1c5e9b3f4
Revises: c3e5a7b9d1f2
Create Date: 2026-10-01 23:30:00.000000
"""

from collections.abc import Sequence
from datetime import UTC
import hashlib

from alembic import op
import sqlalchemy as sa

revision: str = "d7a1c5e9b3f4"
down_revision: str | None = "c3e5a7b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the contract values this revision reads.
_EXHAUSTED = "pr_conflict_repair_exhausted"
_WAITING = "waiting_human_review"
_REFUSAL_KEY = "engineering_dispatch_refusal"
_ADMISSION_KEY = "pr_conflict_repair"
_SUPERSEDING_ACTIONS = {"operator_resume", "pr_conflict_readmit"}
_STOP_BY_REASON = {
    "engineering_budget_denied": "engineering_budget_denied",
    "emergency_stop": "engineering_dispatch_refused",
    "paid_work_limit": "engineering_dispatch_refused",
}


def _repair_task_id(story_id: str, cycle) -> str:
    stamp = cycle.replace(tzinfo=UTC) if cycle.tzinfo is None else cycle.astimezone(UTC)
    identity = f"{story_id}:{stamp.isoformat()}"
    return "pr-conflict-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _pending_refusal(events) -> dict | None:
    saved = None
    for event in events:
        if event.event_type != "status_change":
            continue
        details = event.details or {}
        if event.to_status == _WAITING and _REFUSAL_KEY in details:
            saved = details[_REFUSAL_KEY]
        elif (
            event.from_status == _WAITING
            and event.to_status == "backlog"
            and details.get("action") in _SUPERSEDING_ACTIONS
        ):
            saved = None
    return saved


def _unspent_refusal_stop(connection, story) -> str | None:
    """The stop code this Story's refusal proves, or None when anything differs."""
    task_id = _repair_task_id(story.id, story.reopened_at or story.created_at)
    task = connection.execute(
        sa.text(
            "SELECT id, project_id, story_id, status, created_by, current_iteration "
            "FROM tasks WHERE id = :tid"
        ),
        {"tid": task_id},
    ).one_or_none()
    if (
        task is None
        or task.story_id != story.id
        or task.project_id != story.project_id
        or task.status != _WAITING
        or task.created_by != _ADMISSION_KEY
    ):
        return None
    events = connection.execute(
        sa.text(
            "SELECT event_type, from_status, to_status, details FROM task_events "
            "WHERE task_id = :tid ORDER BY id"
        ).columns(
            sa.column("event_type"),
            sa.column("from_status"),
            sa.column("to_status"),
            sa.column("details", sa.JSON),
        ),
        {"tid": task_id},
    ).all()
    admissions = [e.details[_ADMISSION_KEY] for e in events if _ADMISSION_KEY in (e.details or {})]
    if len(admissions) != 1 or admissions[0].get("pr_number") != story.pr_number:
        return None
    refusal = _pending_refusal(events)
    if not isinstance(refusal, dict) or refusal.get("task_id") != task_id:
        return None
    decision_id = refusal.get("decision_id")
    stop = _STOP_BY_REASON.get(refusal.get("reason"))
    audits = connection.execute(
        sa.text(
            "SELECT reason, outcome, command_payload FROM work_admission_audits "
            "WHERE subject = 'paid_work' AND reference_id = :did"
        ).columns(sa.column("reason"), sa.column("outcome"), sa.column("command_payload", sa.JSON)),
        {"did": decision_id},
    ).all()
    if stop is None or len(audits) != 1:
        return None
    audit = audits[0]
    payload = audit.command_payload or {}
    if (
        audit.reason != refusal["reason"]
        or audit.outcome not in {"denied", "deferred"}
        or payload.get("type") != "engineering"
        or payload.get("project_id") != str(story.project_id)
        or payload.get("story_id") != story.id
        or payload.get("task_id") != task_id
        or (payload.get("run_metadata") or {}).get("iteration") != task.current_iteration
    ):
        return None
    runs = connection.execute(
        sa.text(
            "SELECT id, run_metadata FROM runs "
            "WHERE id = :did OR (task_id = :tid AND type = 'engineering')"
        ).columns(sa.column("id"), sa.column("run_metadata", sa.JSON)),
        {"did": decision_id, "tid": task_id},
    ).all()
    for run in runs:
        metadata = run.run_metadata or {}
        iteration = metadata.get("iteration")
        if run.id == decision_id or (
            not metadata.get("pre_handoff_aborted")
            and type(iteration) is int
            and iteration >= task.current_iteration
        ):
            return None
    # The stop names this refusal; anything restamped later is not this shape.
    detail = story.quarantine_reason.get("detail") or ""
    if task_id not in detail or decision_id not in detail:
        return None
    return stop


def upgrade() -> None:
    connection = op.get_bind()
    stories = connection.execute(
        sa.text(
            "SELECT id, project_id, pr_number, created_at, reopened_at, quarantine_reason "
            "FROM stories WHERE status = :waiting "
            "AND quarantine_reason->>'code' = :exhausted ORDER BY id"
        ).columns(
            sa.column("id"),
            sa.column("project_id"),
            sa.column("pr_number"),
            sa.column("created_at"),
            sa.column("reopened_at"),
            sa.column("quarantine_reason", sa.JSON),
        ),
        {"waiting": _WAITING, "exhausted": _EXHAUSTED},
    ).all()
    for story in stories:
        stop = _unspent_refusal_stop(connection, story)
        if stop is None:
            continue
        connection.execute(
            sa.text("UPDATE stories SET quarantine_reason = :reason WHERE id = :sid").bindparams(
                sa.bindparam("reason", type_=sa.JSON)
            ),
            {"reason": {**story.quarantine_reason, "code": stop}, "sid": story.id},
        )


def downgrade() -> None:
    # The restamped code is the correct record of a no-Run refusal; there is no
    # earlier state worth restoring.
    pass
