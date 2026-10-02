"""Return conflict repairs that a paid refusal mislabelled as exhausted to their unspent shape.

Released code answered a no-Run paid refusal of a PR-conflict repair Task by
parking the Task in human review and stopping its Story as
`pr_conflict_repair_exhausted`, although no repair Run was ever bought. Current
code leaves such a Task in `todo` and stops only the Story with the refusal's own
cause, after which the ordinary repair command is admissible again.

This data migration converts exactly that released shape. Provenance comes from
immutable Task, TaskEvent, paid-audit and Run rows only: the Task's last status
edge is the refusal's own edge, its paid decision bought no Run, and no Run was
bought for its iteration. In released code an exhausted stop on that Story's
current-cycle repair Task can then only stem from that zero-Run refusal: the
attempt settlement writes one only for a Run of the current iteration, and the
repair command's exhaustion only for a Task with no later edge than the stop.
Story text is never read as evidence.

For a match the Task moves `waiting_human_review -> backlog -> todo` with audited
migration edges, its iteration and bound unchanged; the Story stays stopped, but
its stop and both notice texts now name the refusal. Notice delivery state is
kept, so an owed notice delivers the corrected text and nothing is re-sent.
Nothing is dispatched. Every other Story is left alone; a second run finds none.

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

# Frozen copies of the contract values and texts this revision writes or reads.
_EXHAUSTED = "pr_conflict_repair_exhausted"
_WAITING = "waiting_human_review"
_REFUSAL_KEY = "engineering_dispatch_refusal"
_ADMISSION_KEY = "pr_conflict_repair"
_SETTLED = {"done", "cancelled"}
_STOP_BY_REASON = {
    "engineering_budget_denied": "engineering_budget_denied",
    "emergency_stop": "engineering_dispatch_refused",
    "paid_work_limit": "engineering_dispatch_refused",
}
_DETAIL_LIMIT = 500
_OWNER_WORDS = {
    "engineering_budget_denied": (
        "Work on this change stopped before the conflict repair began: the engineering "
        "budget does not cover another attempt, so nothing was spent on it."
    ),
    "engineering_dispatch_refused": (
        "Work on this change stopped before the conflict repair began: the platform did "
        "not start paid engineering work, so nothing was spent on it."
    ),
}


def _repair_task_id(story_id: str, cycle) -> str:
    stamp = cycle.replace(tzinfo=UTC) if cycle.tzinfo is None else cycle.astimezone(UTC)
    identity = f"{story_id}:{stamp.isoformat()}"
    return "pr-conflict-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _usd(microusd) -> str:
    return f"${(microusd or 0) / 1_000_000:,.2f}"


def _bounded(text: str) -> str:
    text = text.strip()
    return text[:_DETAIL_LIMIT] + "..." if len(text) > _DETAIL_LIMIT else text


def _owner_text(code: str, detail: str) -> str:
    cause = f"{_OWNER_WORDS[code]} Cause reported by the scheduler: {detail}"
    if code == "engineering_budget_denied":
        return (
            f"{cause} Nothing more happens automatically; a person has to fix it: "
            "raise the engineering budget, then request the conflict repair again."
        )
    return (
        f"{cause} This is a platform problem, not something the user did. "
        "Nothing more happens automatically; a person has to fix it before the change "
        "can be tried again."
    )


_TIMESTAMPS = ("created_at", "reopened_at")


def _rows(connection, sql: str, params: dict, *json_columns: str):
    """Rows with JSON and timestamp columns typed by name (keyword types match
    result columns by name; positional ones would match by position)."""
    types = {name: sa.DateTime(timezone=True) for name in _TIMESTAMPS if name in sql}
    types.update(dict.fromkeys(json_columns, sa.JSON))
    return connection.execute(sa.text(sql).columns(**types), params).all()


def _refusal_edge(connection, story):
    """The repair Task and refusal edge that prove this Story's stop, or None."""
    task_id = _repair_task_id(story.id, story.reopened_at or story.created_at)
    tasks = _rows(
        connection,
        "SELECT id, project_id, status, created_by, current_iteration FROM tasks "
        "WHERE story_id = :sid",
        {"sid": story.id},
    )
    task = next((row for row in tasks if row.id == task_id), None)
    if (
        task is None
        or task.project_id != story.project_id
        or task.status != _WAITING
        or task.created_by != _ADMISSION_KEY
        or any(row.status not in _SETTLED for row in tasks if row.id != task_id)
    ):
        return None
    events = _rows(
        connection,
        "SELECT event_type, to_status, details, created_at FROM task_events "
        "WHERE task_id = :tid ORDER BY id",
        {"tid": task_id},
        "details",
    )
    admissions = [e.details[_ADMISSION_KEY] for e in events if _ADMISSION_KEY in (e.details or {})]
    edges = [e for e in events if e.event_type == "status_change"]
    if len(admissions) != 1 or admissions[0].get("pr_number") != story.pr_number or not edges:
        return None
    # Nothing changed the Task after the refusal's own edge.
    edge = edges[-1]
    refusal = (edge.details or {}).get(_REFUSAL_KEY)
    if (
        edge.to_status != _WAITING
        or not isinstance(refusal, dict)
        or refusal.get("task_id") != task_id
        or refusal.get("reason") not in _STOP_BY_REASON
    ):
        return None
    decision_id = refusal.get("decision_id")
    audits = _rows(
        connection,
        "SELECT reason, outcome, command_payload FROM work_admission_audits "
        "WHERE subject = 'paid_work' AND reference_id = :did",
        {"did": decision_id},
        "command_payload",
    )
    if len(audits) != 1:
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
    # The ORM attribute run_metadata maps to the column "metadata".
    runs = _rows(
        connection,
        "SELECT id, metadata AS run_metadata FROM runs "
        "WHERE id = :did OR (task_id = :tid AND type = 'engineering')",
        {"did": decision_id, "tid": task_id},
        "run_metadata",
    )
    for run in runs:
        metadata = run.run_metadata or {}
        iteration = metadata.get("iteration")
        if run.id == decision_id or (
            not metadata.get("pre_handoff_aborted")
            and type(iteration) is int
            and iteration >= task.current_iteration
        ):
            return None
    return task, edge, refusal


def _corrected_stop(edge, refusal) -> dict:
    """The refusal's own stop, built from the immutable refusal edge."""
    code = _STOP_BY_REASON[refusal["reason"]]
    detail = edge.details.get("detail") or ""
    budget = edge.details.get("engineering_budget")
    if code == "engineering_budget_denied" and isinstance(budget, dict):
        detail += (
            f" No budget at the decision: spent {_usd(budget.get('known_spend_microusd'))}, "
            f"available {_usd(budget.get('available_microusd'))}; one attempt reserves "
            f"{_usd(budget.get('reservation_microusd'))}; the limit then in force was not "
            "recorded."
        )
    observed = edge.created_at
    observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed
    return {
        "reason": "story_failure",
        "code": code,
        "source": "scheduler",
        "detail": _bounded(detail),
        "observed_at": observed.astimezone(UTC).isoformat(),
    }


def _unspend_task(connection, task, refusal) -> None:
    audit = {
        "migration": revision,
        "decision_id": refusal["decision_id"],
        "cause": "unspent_conflict_repair_attempt",
    }
    for before, after in ((_WAITING, "backlog"), ("backlog", "todo")):
        connection.execute(
            sa.text(
                "INSERT INTO task_events "
                "(task_id, event_type, from_status, to_status, actor, details, created_at) "
                "VALUES (:tid, 'status_change', :before, :after, 'migration', :details, now())"
            ).bindparams(sa.bindparam("details", type_=sa.JSON)),
            {"tid": task.id, "before": before, "after": after, "details": audit},
        )
    (row,) = _rows(
        connection,
        "SELECT failure_metadata FROM tasks WHERE id = :tid",
        {"tid": task.id},
        "failure_metadata",
    )
    connection.execute(
        sa.text(
            "UPDATE tasks SET status = 'todo', failure_metadata = :metadata, updated_at = now() "
            "WHERE id = :tid"
        ).bindparams(sa.bindparam("metadata", type_=sa.JSON)),
        {
            "tid": task.id,
            "metadata": {
                key: value
                for key, value in (row.failure_metadata or {}).items()
                if key not in {_REFUSAL_KEY, "detail", "engineering_budget"}
            },
        },
    )


def upgrade() -> None:
    connection = op.get_bind()
    stories = _rows(
        connection,
        "SELECT id, project_id, pr_number, created_at, reopened_at, owner_notification "
        "FROM stories WHERE status = :waiting "
        "AND quarantine_reason->>'code' = :exhausted ORDER BY id",
        {"waiting": _WAITING, "exhausted": _EXHAUSTED},
        "owner_notification",
    )
    for story in stories:
        proof = _refusal_edge(connection, story)
        if proof is None:
            continue
        task, edge, refusal = proof
        _unspend_task(connection, task, refusal)
        stop = _corrected_stop(edge, refusal)
        notice = story.owner_notification
        if isinstance(notice, dict):
            # Delivery state and episode stay; only the texts name the real cause.
            notice = {
                **notice,
                "text": _owner_text(stop["code"], stop["detail"]),
                "admin_text": (
                    f"Story {story.id} (project {story.project_id}) stopped: "
                    f"{stop['code']} from scheduler: {stop['detail']}"
                ),
            }
        connection.execute(
            sa.text(
                "UPDATE stories SET quarantine_reason = :stop, owner_notification = :notice, "
                "updated_at = now() "
                "WHERE id = :sid"
            ).bindparams(
                sa.bindparam("stop", type_=sa.JSON), sa.bindparam("notice", type_=sa.JSON)
            ),
            {"stop": stop, "notice": notice, "sid": story.id},
        )


def downgrade() -> None:
    # The corrected stop and the unspent Task are the right record of a no-Run
    # refusal; there is no earlier state worth restoring.
    pass
