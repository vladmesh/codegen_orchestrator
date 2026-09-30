"""Real PostgreSQL fences both admitted-start/settlement lock orderings."""

import asyncio

import pytest
from sqlalchemy import select
from test_pr_conflict_repair import dirty_story as _dirty_story

from shared.models import EngineeringBudgetReservation, Project, Run, Story, Task, TaskEvent

dirty_story = _dirty_story
START = "/api/work-admission/engineering-dispatches/start"


async def pending_attempt(client, db, fixture, *, ceiling=False):
    sid, admission, _, _, _ = fixture
    admitted = await client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=admission)
    assert admitted.status_code == 200, admitted.text
    tid = admitted.json()["task_id"]
    task = await db.get(Task, tid, populate_existing=True)
    project = await db.get(Project, task.project_id)
    project.config = {**project.config, "workspace_ready": True}
    if ceiling:
        task.current_iteration = task.max_iterations
    await db.commit()
    admitted = await client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": tid}
    )
    assert admitted.json()["outcome"] == "admitted", admitted.text
    rid = admitted.json()["run_id"]
    command = {
        "project_id": admission["project_id"],
        "pr_number": 3,
        "cycle_started_at": admission["cycle_started_at"],
        "task_id": tid,
        "attempt_id": rid,
        "expected_iteration": task.current_iteration,
        "disposition": "failed",
        "detail": "Persisted attempt decides",
    }
    return sid, {"task_id": tid, "run_id": rid}, command


async def snapshot(db, sid, tid):
    task = await db.get(Task, tid, populate_existing=True)
    story = await db.get(Story, sid, populate_existing=True)
    runs = (
        await db.scalars(
            select(Run)
            .where(Run.task_id == tid)
            .order_by(Run.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    holds = (
        await db.scalars(
            select(EngineeringBudgetReservation)
            .execution_options(populate_existing=True)
            .where(EngineeringBudgetReservation.attempt_id.in_([r.id for r in runs]))
        )
    ).all()
    events = (
        await db.scalars(select(TaskEvent).where(TaskEvent.task_id == tid).order_by(TaskEvent.id))
    ).all()
    return (
        task.status,
        task.current_iteration,
        story.status,
        story.quarantine_reason,
        story.owner_notification,
        [(r.id, r.status, r.run_metadata, r.result) for r in runs],
        [(h.id, h.state, h.active_held_microusd) for h in holds],
        [e.id for e in events],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("start_first", [False, True])
async def test_delayed_start_preserves_atomic_retry_and_new_live_attempt(
    async_client, db_session, dirty_story, start_first
):
    sid, start, command = await pending_attempt(async_client, db_session, dirty_story)
    if start_first:
        # A committed start response is lost; duplicate requests replay it.
        ignored = await async_client.post(START, json=start)
        assert ignored.json()["outcome"] == "started", ignored.text
        replies = await asyncio.gather(*[async_client.post(START, json=start) for _ in range(4)])
        assert all(r.json()["outcome"] == "reused" for r in replies)
    failed = await async_client.patch(
        f"/api/runs/{start['run_id']}",
        json={"status": "failed", "result": {"engineering_status": "failed"}},
    )
    assert failed.status_code == 200, failed.text
    pending = await async_client.post(START, json=start)
    assert pending.json()["outcome"] == "terminal_pending", pending.text
    url = f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome"
    # Both lock contenders use independent real endpoint sessions.
    settled, delayed = await asyncio.gather(
        async_client.post(url, json=command), async_client.post(START, json=start)
    )
    assert settled.json()["outcome"] == "retried", settled.text
    assert delayed.json()["outcome"] in {"settled", "terminal_pending"}, delayed.text
    # Ignore the committed settlement response, then replay it.
    assert (await async_client.post(url, json=command)).json()["outcome"] == "reused"
    before = await snapshot(db_session, sid, start["task_id"])
    assert before[:3] == ("todo", 1, "in_progress")
    assert before[4] is None
    replies = await asyncio.gather(*[async_client.post(START, json=start) for _ in range(4)])
    assert all(r.json()["outcome"] == "settled" for r in replies)
    assert await snapshot(db_session, sid, start["task_id"]) == before
    # Normal admission creates exactly one next attempt despite a replay.
    admitted = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": start["task_id"]}
    )
    assert admitted.json()["outcome"] == "admitted", admitted.text
    new_id = admitted.json()["run_id"]
    replay = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": start["task_id"]}
    )
    assert replay.json()["run_id"] == new_id and replay.json()["outcome"] == "repair"
    response = await async_client.post(START, json={"task_id": start["task_id"], "run_id": new_id})
    assert response.json()["outcome"] == "started", response.text
    before = await snapshot(db_session, sid, start["task_id"])
    assert len(before[5]) == 2 and before[:2] == ("in_dev", 1)
    await asyncio.gather(*[async_client.post(START, json=start) for _ in range(4)])
    assert await snapshot(db_session, sid, start["task_id"]) == before
    events = (
        await db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == start["task_id"]))
    ).all()
    assert sum("pr_conflict_repair_attempt" in e.details for e in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
async def test_terminal_repair_late_start_cannot_revive_or_repeat_notice(
    async_client, db_session, dirty_story, gave_up
):
    sid, start, command = await pending_attempt(
        async_client, db_session, dirty_story, ceiling=not gave_up
    )
    await async_client.patch(
        f"/api/runs/{start['run_id']}",
        json={
            "status": "failed",
            "result": {"engineering_status": "gave_up" if gave_up else "failed"},
        },
    )
    ended = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    assert ended.json()["outcome"] == "exhausted", ended.text
    before = await snapshot(db_session, sid, start["task_id"])
    assert before[0] == before[2] == "waiting_human_review"
    assert before[4]["state"] == before[4]["admin_state"] == "owed"
    responses = await asyncio.gather(*[async_client.post(START, json=start) for _ in range(4)])
    assert all(r.json()["outcome"] == "settled" for r in responses)
    assert await snapshot(db_session, sid, start["task_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["cycle", "pr", "foreign", "malformed"])
async def test_old_or_unrelated_start_cannot_mutate_replaced_work(
    async_client, db_session, dirty_story, replacement
):
    sid, start, _ = await pending_attempt(async_client, db_session, dirty_story)
    if replacement == "cycle":
        for action in ("fail", "reopen", "start"):
            moved = await async_client.post(f"/api/stories/{sid}/{action}")
            assert moved.status_code == 200, moved.text
    elif replacement == "pr":
        await async_client.patch(f"/api/stories/{sid}", json={"pr_number": 4})
    elif replacement == "foreign":
        start["run_id"] = "foreign-run"
    else:
        run = await db_session.get(Run, start["run_id"])
        run.run_metadata = {**run.run_metadata, "iteration": True}
        await db_session.commit()
    before = await snapshot(db_session, sid, start["task_id"])
    response = await async_client.post(START, json=start)
    if replacement in {"foreign", "malformed"}:
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200 and response.json()["outcome"] == "stale", response.text
    assert await snapshot(db_session, sid, start["task_id"]) == before


@pytest.mark.asyncio
async def test_generic_start_and_transition_do_not_bypass_attempt_fence(
    async_client, db_session, dirty_story
):
    sid, start, _ = await pending_attempt(async_client, db_session, dirty_story)
    before = await snapshot(db_session, sid, start["task_id"])
    for suffix in ("start", "transition?to_status=in_dev"):
        response = await async_client.post(
            f"/api/tasks/{start['task_id']}/{suffix}", json={"actor": "dispatcher"}
        )
        assert response.status_code == 409, response.text
    assert await snapshot(db_session, sid, start["task_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", ["infrastructure", "resource"])
async def test_proven_priority_restores_only_fenced_discovery(
    async_client, db_session, dirty_story, priority
):
    sid, start, command = await pending_attempt(async_client, db_session, dirty_story)
    result = {
        "engineering_status": "failed",
        **(
            {
                "execution": {
                    "execution_phase": "pre_agent_refused",
                    "infrastructure_refusal": "project_locked",
                }
            }
            if priority == "infrastructure"
            else {"allocation_failure_reason": "impossible_capacity"}
        ),
    }
    response = await async_client.patch(
        f"/api/runs/{start['run_id']}", json={"status": "failed", "result": result}
    )
    assert response.status_code == 200, response.text
    restored = await async_client.post(START, json=start)
    assert restored.json()["outcome"] == "priority_pending", restored.text
    before = await snapshot(db_session, sid, start["task_id"])
    assert before[:3] == ("in_dev", 0, "in_progress")
    assert before[4] is None
    for _ in range(3):
        replay = await async_client.post(START, json=start)
        assert replay.json()["outcome"] == "priority_pending", replay.text
    assert await snapshot(db_session, sid, start["task_id"]) == before
    # Product retry settlement may never consume this refusal or its bound.
    refused = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    assert refused.status_code == 409, refused.text
    assert await snapshot(db_session, sid, start["task_id"]) == before
