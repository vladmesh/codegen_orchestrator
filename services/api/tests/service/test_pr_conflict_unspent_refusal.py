"""A paid refusal before any Run leaves the conflict repair attempt unspent."""

import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select
from test_pr_conflict_dispatch_refusal import DISPATCH, decisions, history, ready_task
from test_pr_conflict_repair import dirty_story as _dirty_story

from shared.models import Run, Story, Task

dirty_story = _dirty_story
MIGRATION = "d7a1c5e9b3f4_restamp_unspent_conflict_refusal_stops.py"


async def refuse(client, db, fixture, *, later=False):
    sid, tid, policy_url, version = await ready_task(client, db, fixture, later)
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


async def give_up(client, db, tid):
    """A real repair Run starts and ends gave_up: the attempt is spent."""
    sid = (await db.get(Task, tid, populate_existing=True)).story_id
    admitted = await client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    rid = admitted.json()["run_id"]
    started = await client.post(f"{DISPATCH}/start", json={"task_id": tid, "run_id": rid})
    assert started.json()["outcome"] == "started", started.text
    failed = await client.patch(
        f"/api/runs/{rid}",
        json={"status": "failed", "result": {"engineering_status": "gave_up"}},
    )
    assert failed.status_code == 200, failed.text
    run = await db.get(Run, rid, populate_existing=True)
    task = await db.get(Task, tid, populate_existing=True)
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
    assert task.current_iteration == run.run_metadata["iteration"]
    return rid


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True], ids=["iteration0", "bounded-retry"])
async def test_budget_refusal_says_no_budget_and_top_up_readmits_in_the_same_cycle(
    async_client, db_session, dirty_story, later
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story, later=later)
    command = dirty_story[1]
    story = await db_session.get(Story, sid, populate_existing=True)
    task = await db_session.get(Task, tid, populate_existing=True)
    cycle, bound, iteration = story.reopened_at, task.max_iterations, task.current_iteration
    assert story.status == task.status == "waiting_human_review"
    reason = story.quarantine_reason
    assert reason["code"] == "engineering_budget_denied", reason
    assert "No budget: limit $0.00, spent $0.00" in reason["detail"]
    notice = story.owner_notification
    assert "engineering budget" in notice["text"] and "after repair" not in notice["text"]
    assert "request the conflict repair again" in notice["text"]
    (did,) = [a.reference_id for a in await decisions(db_session, tid)]
    runs_before, _ = await history(db_session, tid)
    # The forged client edge cannot claim the native re-admission action.
    forged = await async_client.post(
        f"/api/tasks/{tid}/transition",
        params={"to_status": "backlog"},
        json={"actor": "owner", "details": {"action": "pr_conflict_readmit"}},
    )
    assert forged.status_code == 409, forged.text

    await top_up(async_client, policy_url, version)
    repaired = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["outcome"] == "admitted" and repaired.json()["task_id"] == tid
    # A lost response repeats harmlessly.
    again = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert again.json()["outcome"] == "reused", again.text
    story = await db_session.get(Story, sid, populate_existing=True)
    task = await db_session.get(Task, tid, populate_existing=True)
    assert (story.status, story.quarantine_reason, story.reopened_at) == (
        "in_progress",
        None,
        cycle,
    )
    assert (task.status, task.current_iteration, task.max_iterations) == ("todo", iteration, bound)
    assert "engineering_dispatch_refusal" not in task.failure_metadata
    admitted = await async_client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    run = await db_session.get(Run, admitted.json()["run_id"])
    assert run.run_metadata["iteration"] == iteration and run.id != did
    runs_after, _ = await history(db_session, tid)
    assert all(run in runs_after for run in runs_before)
    tasks = (await db_session.scalars(select(Task).where(Task.story_id == sid))).all()
    assert sum(t.id.startswith("pr-conflict-") for t in tasks) == 1


@pytest.mark.asyncio
async def test_a_real_repair_run_spends_the_attempt_and_the_next_command_is_exhausted(
    async_client, db_session, dirty_story
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    await top_up(async_client, policy_url, version)
    command = dirty_story[1]
    readmitted = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert readmitted.json()["outcome"] == "admitted", readmitted.text
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
async def test_migration_restamps_the_stuck_refusal_and_the_standard_command_then_works(
    async_client, db_session, dirty_story
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    # The shape released code left: the refusal's Story stop labelled as exhausted.
    story = await db_session.get(Story, sid, populate_existing=True)
    legacy = {**story.quarantine_reason, "code": "pr_conflict_repair_exhausted"}
    story.quarantine_reason = legacy
    await db_session.commit()
    await top_up(async_client, policy_url, version)
    command = dirty_story[1]
    stuck = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert stuck.json()["outcome"] == "exhausted", stuck.text
    before = await history(db_session, tid)

    await run_migration(db_session)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.quarantine_reason == {**legacy, "code": "engineering_budget_denied"}
    assert story.status == "waiting_human_review"
    assert await history(db_session, tid) == before
    await run_migration(db_session)
    assert (await db_session.get(Story, sid, populate_existing=True)).quarantine_reason == {
        **legacy,
        "code": "engineering_budget_denied",
    }

    repaired = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert repaired.json()["outcome"] == "admitted", repaired.text
    admitted = await async_client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["real_run", "unrelated_stop", "resumed_then_spent"])
async def test_migration_leaves_every_other_stop_alone(
    async_client, db_session, dirty_story, shape
):
    sid, tid, policy_url, version = await refuse(async_client, db_session, dirty_story)
    command = dirty_story[1]
    await top_up(async_client, policy_url, version)
    if shape == "unrelated_stop":
        # Exhaustion text on a Story whose Task never left its refusal and has
        # no matching decision in the stop: not this card's proven shape.
        story = await db_session.get(Story, sid, populate_existing=True)
        story.quarantine_reason = {
            **story.quarantine_reason,
            "code": "pr_conflict_repair_exhausted",
            "detail": "Unrelated operator stop",
        }
        await db_session.commit()
    elif shape == "real_run":
        readmitted = await async_client.post(
            f"/api/stories/{sid}/repair-pr-conflicts", json=command
        )
        assert readmitted.json()["outcome"] == "admitted", readmitted.text
        await give_up(async_client, db_session, tid)
    else:
        from test_pr_conflict_dispatch_refusal import bearer

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
    story = await db_session.get(Story, sid, populate_existing=True)
    before = (story.status, story.quarantine_reason, story.owner_notification)
    runs = await history(db_session, tid)
    await run_migration(db_session)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert (story.status, story.quarantine_reason, story.owner_notification) == before
    assert await history(db_session, tid) == runs
    if shape != "unrelated_stop":
        exhausted = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
        assert exhausted.json()["outcome"] == "exhausted", exhausted.text
