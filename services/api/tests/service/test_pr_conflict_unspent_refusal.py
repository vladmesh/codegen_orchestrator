"""A paid refusal before any Run leaves the conflict repair attempt unspent."""

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, select
from test_pr_conflict_dispatch_refusal import DISPATCH, bearer, decisions, history, ready_task
from test_pr_conflict_repair import dirty_story as _dirty_story

from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    StoryFailure,
    StoryFailureCode,
    story_failure_admin_text,
    story_failure_owner_text,
)
from shared.models import Project, Run, Story, Task, TaskEvent, WorkAdmissionAudit

dirty_story = _dirty_story
MIGRATION = "d7a1c5e9b3f4_unspend_released_conflict_refusals.py"
REFUSAL = "engineering_dispatch_refusal"


async def refuse(client, db, fixture):
    sid, tid, policy_url, version = await ready_task(client, db, fixture, False)
    refused = await client.post(DISPATCH, json={"task_id": tid})
    assert refused.json()["reason"] == "engineering_budget_denied", refused.text
    assert refused.json()["refusal_disposition"]["task_id"] == tid
    return sid, tid, policy_url, version


async def top_up(client, policy_url, version):
    restored = await client.put(
        policy_url,
        json={
            "limit_microusd": 200_000_000,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": version,
        },
    )
    assert restored.status_code == 200, restored.text


async def released_shape(db, sid, tid):
    """Rewrite a current refusal into exactly what released code committed for it.

    Released `_dispose_conflict_refusal` moved the Task `todo -> in_dev ->
    waiting_human_review` with the refusal audit on both edges and in its
    failure metadata, and stopped the Story as exhausted with owed notices.
    The real paid audit, budget reservation and Run absence stay as decided.
    """
    from src.routers._story_helpers import _record_story_failure

    note = await db.scalar(
        select(TaskEvent)
        .where(TaskEvent.task_id == tid, TaskEvent.event_type == "note")
        .order_by(TaskEvent.id.desc())
    )
    assert REFUSAL in note.details
    decision = note.details[REFUSAL]
    audit_row = await db.scalar(
        select(WorkAdmissionAudit).where(WorkAdmissionAudit.reference_id == decision["decision_id"])
    )
    task = await db.get(Task, tid, populate_existing=True)
    story = await db.get(Story, sid, populate_existing=True)
    detail = (
        f"PR #{story.pr_number}: repair Task {tid}, decision {decision['decision_id']}, "
        f"iteration {task.current_iteration}, ceiling {task.max_iterations}: "
        f"{decision['reason']}. {audit_row.message}"
    )
    audit = {
        REFUSAL: decision,
        "detail": detail,
        "engineering_budget": note.details["engineering_budget"],
    }
    await db.execute(delete(TaskEvent).where(TaskEvent.id == note.id))
    for before, after, actor in (
        ("todo", "in_dev", "internal_service"),
        ("in_dev", "waiting_human_review", "dispatcher"),
    ):
        db.add(
            TaskEvent(
                task_id=tid,
                event_type="status_change",
                from_status=before,
                to_status=after,
                actor=actor,
                details=audit,
            )
        )
    task.status = "waiting_human_review"
    task.failure_metadata = {**(task.failure_metadata or {}), **audit}
    _record_story_failure(
        story,
        StoryFailure(
            code=StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED, source="scheduler", detail=detail
        ),
        StoryStatus.WAITING_HUMAN_REVIEW,
    )
    await db.commit()
    return detail, audit


async def run_migration(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[2] / "migrations/versions" / MIGRATION
    spec = importlib.util.spec_from_file_location("unspent_refusal_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(session):
        original = migration.op
        migration.op = Operations(MigrationContext.configure(session.connection()))
        try:
            migration.upgrade()
        finally:
            migration.op = original

    await db.run_sync(exercise)
    await db.commit()


async def consumer_takes_up(client, rid):
    """The engineering consumer's own first write: the Run's work began."""
    taken = await client.patch(
        f"/api/runs/{rid}",
        json={"status": "running", "started_at": datetime.now(UTC).isoformat()},
    )
    assert taken.status_code == 200, taken.text


async def give_up(client, db, tid):
    """A real repair Run is admitted, starts and ends gave_up: the attempt is spent."""
    sid = (await db.get(Task, tid, populate_existing=True)).story_id
    admitted = await client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    rid = admitted.json()["run_id"]
    started = await client.post(f"{DISPATCH}/start", json={"task_id": tid, "run_id": rid})
    assert started.json()["outcome"] == "started", started.text
    await consumer_takes_up(client, rid)
    failed = await client.patch(
        f"/api/runs/{rid}",
        json={"status": "failed", "result": {"engineering_status": "gave_up"}},
    )
    assert failed.status_code == 200, failed.text
    run = await db.get(Run, rid, populate_existing=True)
    story = await db.get(Story, sid, populate_existing=True)
    events = (await client.get(f"/api/tasks/{tid}/events")).json()
    admission = next(
        e["details"]["pr_conflict_repair"] for e in events if "pr_conflict_repair" in e["details"]
    )
    settled = await client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome",
        json={
            "project_id": str(story.project_id),
            "pr_number": story.pr_number,
            "cycle_started_at": admission["cycle_started_at"],
            "task_id": tid,
            "attempt_id": rid,
            "expected_iteration": run.run_metadata["iteration"],
            "disposition": "gave_up",
            "detail": "Synthetic gave-up conflict attempt",
        },
    )
    assert settled.json()["outcome"] == "exhausted", settled.text
    return rid


@pytest.mark.asyncio
async def test_a_real_repair_run_spends_the_attempt_and_the_next_command_is_exhausted(
    async_client, db_session, dirty_story
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    await top_up(async_client, policy_url, version)
    command = dirty_story[1]
    resumed = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert resumed.json()["outcome"] == "reused", resumed.text
    rid = await give_up(async_client, db_session, tid)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.quarantine_reason["code"] == "pr_conflict_repair_exhausted"
    before = await history(db_session, tid)
    exhausted = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert exhausted.json()["outcome"] == "exhausted", exhausted.text
    assert rid in exhausted.json()["reason"]
    assert await history(db_session, tid) == before
    task = await db_session.get(Task, tid, populate_existing=True)
    assert task.status == "waiting_human_review"
    refused = await async_client.post(DISPATCH, json={"task_id": tid})
    assert refused.json()["reason"] == "task_not_dispatchable", refused.text


@pytest.mark.asyncio
@pytest.mark.parametrize("taken_up", [False, True], ids=["never-started", "started"])
async def test_only_a_started_repair_run_spends_the_attempt(
    async_client, db_session, dirty_story, taken_up
):
    sid, command, _, _, _ = dirty_story
    url = f"/api/stories/{sid}/repair-pr-conflicts"
    tid = (await async_client.post(url, json=command)).json()["task_id"]
    task = await db_session.get(Task, tid)
    project = await db_session.get(Project, task.project_id)
    project.config = {**project.config, "workspace_ready": True}
    await db_session.commit()
    # Admission buys the Run and its hold; no consumer has taken it up yet.
    admitted = await async_client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    rid = admitted.json()["run_id"]
    if taken_up:
        await consumer_takes_up(async_client, rid)
    # An operator cancels the Run and its Task through the existing routes.
    cancelled = await async_client.patch(f"/api/runs/{rid}", json={"status": "cancelled"})
    assert cancelled.status_code == 200, cancelled.text
    removed = await async_client.delete(f"/api/tasks/{tid}")
    assert removed.status_code in {200, 204}, removed.text
    assert (await db_session.get(Task, tid, populate_existing=True)).status == "cancelled"
    response = await async_client.post(url, json=command)
    story = await db_session.get(Story, sid, populate_existing=True)
    if taken_up:
        assert response.json()["outcome"] == "exhausted", response.text
        assert story.quarantine_reason["code"] == "pr_conflict_repair_exhausted"
    else:
        # Admission without a start is no attempt: never labelled exhausted.
        assert response.status_code == 409, response.text
        assert "before any repair Run started" in response.json()["detail"]["message"]
        assert (story.status, story.quarantine_reason, story.owner_notification) == (
            "in_progress",
            None,
            None,
        )


@pytest.mark.asyncio
async def test_migration_returns_the_released_stuck_shape_to_an_unspent_attempt(
    async_client, db_session, dirty_story
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    detail, audit = await released_shape(db_session, sid, tid)
    await top_up(async_client, policy_url, version)
    command = dirty_story[1]
    # Released data is stuck: the ordinary command answers exhausted and writes nothing.
    stuck = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert stuck.json()["outcome"] == "exhausted", stuck.text
    runs, events = await history(db_session, tid)
    story = await db_session.get(Story, sid, populate_existing=True)
    owed = story.owner_notification
    assert "still has conflicts after repair" in owed["text"]

    await run_migration(db_session)
    task = await db_session.get(Task, tid, populate_existing=True)
    assert (task.status, task.current_iteration) == ("todo", 0)
    assert REFUSAL not in task.failure_metadata and "pr_conflict_repair" in task.failure_metadata
    migrated_runs, migrated_events = await history(db_session, tid)
    assert migrated_runs == runs and migrated_events[: len(events)] == events
    added = migrated_events[len(events) :]
    assert [e[1]["migration"] for e in added] == ["d7a1c5e9b3f4", "d7a1c5e9b3f4"]
    assert all(e[1]["decision_id"] == audit[REFUSAL]["decision_id"] for e in added)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "waiting_human_review"
    failure = StoryFailure.model_validate(story.quarantine_reason)
    assert failure.code is StoryFailureCode.ENGINEERING_BUDGET_DENIED
    assert failure.detail.startswith(detail) and "No budget at the decision: spent $0.00" in (
        failure.detail
    )
    # Both audiences now name the refusal, in exactly the native texts, and
    # keep their delivery state: the owed notice delivers the corrected cause.
    notice = story.owner_notification
    assert notice["text"] == story_failure_owner_text(failure)
    assert notice["admin_text"] == story_failure_admin_text(sid, str(story.project_id), failure)
    assert "after repair" not in notice["text"] and "engineering budget" in notice["text"]
    assert {k: v for k, v in notice.items() if k not in {"text", "admin_text"}} == {
        k: v for k, v in owed.items() if k not in {"text", "admin_text"}
    }
    snapshot = (story.quarantine_reason, story.owner_notification, await history(db_session, tid))
    await run_migration(db_session)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert (
        story.quarantine_reason,
        story.owner_notification,
        await history(db_session, tid),
    ) == snapshot

    repaired = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert repaired.json()["outcome"] == "reused", repaired.text
    admitted = await async_client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    run = await db_session.get(Run, admitted.json()["run_id"])
    assert run.run_metadata["iteration"] == 0
    assert [a.reference_id for a in await decisions(db_session, tid)] == [
        audit[REFUSAL]["decision_id"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["real_run", "resumed_then_spent", "unsettled_sibling"])
async def test_migration_leaves_every_other_history_alone(
    async_client, db_session, dirty_story, shape
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    command = dirty_story[1]
    await top_up(async_client, policy_url, version)
    if shape == "real_run":
        resumed = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
        assert resumed.json()["outcome"] == "reused", resumed.text
        await give_up(async_client, db_session, tid)
    elif shape == "resumed_then_spent":
        # The released stuck shape, recovered by the native operator resume and
        # then genuinely exhausted by a real Run.
        await released_shape(db_session, sid, tid)
        admin = await async_client.post(
            "/api/users/", json={"telegram_id": uuid.uuid4().int % 1_000_000_000, "is_admin": True}
        )
        assert admin.status_code == 201, admin.text
        resumed = await async_client.post(
            f"/api/tasks/{tid}/resume",
            headers=bearer(admin.json()["id"]),
            json={"guidance": "Budget restored", "retries": 1},
        )
        assert resumed.status_code == 200, resumed.text
        await give_up(async_client, db_session, tid)
    else:
        # The released stuck shape, but other cycle work is not settled.
        await released_shape(db_session, sid, tid)
        other = await async_client.post(
            "/api/tasks/",
            json={
                "project_id": command["project_id"],
                "story_id": sid,
                "title": "Unsettled work",
                "status": "todo",
            },
        )
        assert other.status_code in {200, 201}, other.text
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.quarantine_reason["code"] == "pr_conflict_repair_exhausted"
    before = (story.status, story.quarantine_reason, story.owner_notification)
    task_before = await db_session.get(Task, tid, populate_existing=True)
    task_state = (task_before.status, task_before.failure_metadata)
    runs = await history(db_session, tid)
    await run_migration(db_session)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert (story.status, story.quarantine_reason, story.owner_notification) == before
    task = await db_session.get(Task, tid, populate_existing=True)
    assert (task.status, task.failure_metadata) == task_state
    assert await history(db_session, tid) == runs
    exhausted = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert exhausted.json()["outcome"] == "exhausted", exhausted.text
