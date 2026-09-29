"""Attempt settlement fences and atomic retries on real PostgreSQL."""

import asyncio

import pytest
from test_pr_conflict_repair import dirty_story as _dirty_story

from shared.contracts.dto.pr_conflict_repair import PRConflictRepairAttemptDisposition
from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.models import Project, Run, Story, Task
from shared.pr_conflict_repair import settle_pr_repair_attempt

dirty_story = _dirty_story


async def failed_attempt(client, db, fixture, *, ceiling=False, gave_up=False):
    sid, admission, _, _, _ = fixture
    admitted = await client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=admission)
    assert admitted.status_code == 200, admitted.text
    tid = admitted.json()["task_id"]
    task = await db.get(Task, tid)
    project = await db.get(Project, task.project_id)
    project.config = {**project.config, "workspace_ready": True}
    if ceiling:
        task.current_iteration = task.max_iterations
    await db.commit()
    response = await client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": tid}
    )
    assert response.json()["outcome"] == "admitted", response.text
    rid = response.json()["run_id"]
    response = await client.post(f"/api/tasks/{tid}/start")
    assert response.status_code == 200, response.text
    response = await client.patch(
        f"/api/runs/{rid}",
        json={
            "status": "failed",
            "result": {"engineering_status": "gave_up" if gave_up else "failed"},
        },
    )
    assert response.status_code == 200, response.text
    if not gave_up:
        response = await client.post(f"/api/tasks/{tid}/fail")
        assert response.status_code == 200, response.text
    return sid, {
        "project_id": admission["project_id"],
        "pr_number": 3,
        "cycle_started_at": admission["cycle_started_at"],
        "task_id": tid,
        "attempt_id": rid,
        "expected_iteration": task.current_iteration,
        "disposition": "gave_up" if gave_up else "failed",
        "detail": "Synthetic failed conflict attempt",
    }


@pytest.mark.asyncio
async def test_retry_is_atomic_concurrent_and_response_loss_replay(
    async_client, db_session, dirty_story
):
    sid, command = await failed_attempt(async_client, db_session, dirty_story)
    url = f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome"
    replies = await asyncio.gather(*[async_client.post(url, json=command) for _ in range(6)])
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert sum(r.json()["outcome"] == "retried" for r in replies) == 1
    # The first committed response may be lost. Replaying the command has no increment.
    replay = await async_client.post(url, json=command)
    assert replay.json()["outcome"] == "reused", replay.text
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert (task.status, task.current_iteration) == ("todo", 1)
    dispatched = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": task.id}
    )
    assert dispatched.json()["outcome"] == "admitted", dispatched.text
    assert dispatched.json()["run_id"] != command["attempt_id"]
    assert (await async_client.post(url, json=command)).json()["outcome"] == "reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
async def test_terminal_settlement_and_lost_response_owe_each_audience_once(
    async_client, db_session, dirty_story, gave_up
):
    sid, command = await failed_attempt(
        async_client, db_session, dirty_story, ceiling=True, gave_up=gave_up
    )
    url = f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome"
    ended = await async_client.post(url, json=command)
    assert ended.status_code == 200, ended.text
    assert ended.json()["outcome"] == "exhausted"
    story = await db_session.get(Story, sid, populate_existing=True)
    notice = story.owner_notification
    assert notice["state"] == notice["admin_state"] == "owed"
    assert command["attempt_id"] in story.quarantine_reason["detail"]
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert task.status == "waiting_human_review"
    assert (await async_client.post(url, json=command)).json()["outcome"] == "exhausted"
    await db_session.refresh(story)
    assert story.owner_notification == notice


@pytest.mark.asyncio
async def test_reconcile_preexisting_named_stop_finishes_task_without_new_notice(
    async_client, db_session, dirty_story
):
    sid, command = await failed_attempt(async_client, db_session, dirty_story, ceiling=True)
    failure = StoryFailure(
        code=StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED,
        source="scheduler",
        detail=(
            f"PR #3: repair Task {command['task_id']}, Run {command['attempt_id']}, "
            f"ceiling {command['expected_iteration']}. Repair exhausted."
        ),
    )
    stopped = await async_client.post(
        f"/api/stories/{sid}/human-review",
        json={"actor": "scheduler", "failure": failure.model_dump(mode="json")},
    )
    assert stopped.status_code == 200, stopped.text
    story = await db_session.get(Story, sid, populate_existing=True)
    notice = story.owner_notification
    reason = story.quarantine_reason
    url = f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome"
    ended = await async_client.post(url, json=command)
    assert ended.status_code == 200 and ended.json()["outcome"] == "exhausted", ended.text
    assert (await async_client.post(url, json=command)).json()["outcome"] == "exhausted"
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert task.status == "waiting_human_review"
    await db_session.refresh(story)
    assert story.owner_notification == notice and story.quarantine_reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
async def test_old_terminal_callback_cannot_stop_normally_reopened_cycle(
    async_client, db_session, dirty_story, gave_up
):
    sid, command = await failed_attempt(
        async_client, db_session, dirty_story, ceiling=True, gave_up=gave_up
    )
    for action in ("fail", "reopen", "start"):
        moved = await async_client.post(f"/api/stories/{sid}/{action}")
        assert moved.status_code == 200, moved.text
    story = await db_session.get(Story, sid, populate_existing=True)
    before = (story.status, story.reopened_at, story.quarantine_reason, story.owner_notification)
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "stale"
    await db_session.refresh(story)
    assert (
        story.status,
        story.reopened_at,
        story.quarantine_reason,
        story.owner_notification,
    ) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["iteration", "attempt", "pr"])
async def test_stale_attempt_identity_mutates_nothing(
    async_client, db_session, dirty_story, mismatch
):
    sid, command = await failed_attempt(async_client, db_session, dirty_story)
    if mismatch == "iteration":
        command["expected_iteration"] += 1
    elif mismatch == "attempt":
        command["attempt_id"] = "foreign-attempt"
    else:
        command["pr_number"] += 1
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "stale"
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert (task.status, task.current_iteration) == ("failed", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
@pytest.mark.parametrize("replacement", ["cycle", "pr"])
async def test_client_reads_interleaved_with_normal_lifecycle_cannot_authorize_old_stop(
    async_client, db_session, dirty_story, gave_up, replacement
):
    sid, command = await failed_attempt(
        async_client, db_session, dirty_story, ceiling=True, gave_up=gave_up
    )
    snapshots = []

    class InterleavedAPI:
        async def request(self, method, path, **kwargs):
            response = await async_client.request(method, f"/api/{path}", **kwargs)
            response.raise_for_status()
            if method == "GET" and path.endswith("/events"):
                if replacement == "cycle":
                    for action in ("fail", "reopen", "start"):
                        moved = await async_client.post(f"/api/stories/{sid}/{action}")
                        assert moved.status_code == 200, moved.text
                else:
                    moved = await async_client.patch(f"/api/stories/{sid}", json={"pr_number": 4})
                    assert moved.status_code == 200, moved.text
                row = await db_session.get(Story, sid, populate_existing=True)
                snapshots.append(
                    (row.status, row.reopened_at, row.quarantine_reason, row.owner_notification)
                )
            return response

        async def get_run(self, rid):
            return RunDTO.model_validate((await async_client.get(f"/api/runs/{rid}")).json())

    outcome = await settle_pr_repair_attempt(
        InterleavedAPI(),
        sid,
        command["task_id"],
        command["attempt_id"],
        "Old repair exhausted",
        PRConflictRepairAttemptDisposition.GAVE_UP
        if gave_up
        else PRConflictRepairAttemptDisposition.FAILED,
    )
    assert outcome.outcome.value == "stale"
    row = await db_session.get(Story, sid, populate_existing=True)
    assert (
        row.status,
        row.reopened_at,
        row.quarantine_reason,
        row.owner_notification,
    ) == snapshots[0]
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert task.status == ("in_dev" if gave_up else "failed")
    assert task.current_iteration == command["expected_iteration"]


@pytest.mark.asyncio
async def test_unreleased_partial_backlog_refuses_without_mutation(
    async_client, db_session, dirty_story
):
    sid, command = await failed_attempt(async_client, db_session, dirty_story, ceiling=True)
    response = await async_client.post(
        f"/api/tasks/{command['task_id']}/transition",
        params={"to_status": "backlog"},
        json={"actor": "supervisor"},
    )
    assert response.status_code == 200, response.text
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    assert response.status_code == 409, response.text
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    assert task.current_iteration == command["expected_iteration"]
    assert task.status == "backlog"
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "in_progress" and story.owner_notification is None


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
@pytest.mark.parametrize("task_status", ["in_dev", "todo"])
async def test_immutable_run_outranks_callback_and_settles_before_task_write(
    async_client, db_session, dirty_story, gave_up, task_status
):
    from sqlalchemy import select

    from shared.models import TaskEvent

    sid, command = await failed_attempt(async_client, db_session, dirty_story, gave_up=True)
    run = await db_session.get(Run, command["attempt_id"], populate_existing=True)
    run.result = {"engineering_status": "gave_up" if gave_up else "failed"}
    run.error_message = "Durable refusal" if gave_up else "Durable technical failure"
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    task.status = task_status
    await db_session.commit()
    # The command's mutable observation disagrees with immutable terminal evidence.
    command["disposition"] = "failed" if gave_up else "gave_up"
    command["detail"] = "Stale callback-local diagnostic"
    url = f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome"
    replies = await asyncio.gather(*[async_client.post(url, json=command) for _ in range(6)])
    assert all(reply.status_code == 200 for reply in replies), [r.text for r in replies]
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    story = await db_session.get(Story, sid, populate_existing=True)
    events = (await db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
    settlements = [
        e.details["pr_conflict_repair_attempt"]
        for e in events
        if "pr_conflict_repair_attempt" in e.details
    ]
    assert len(settlements) == 1
    assert settlements[0]["disposition"] == run.result["engineering_status"]
    if gave_up:
        assert all(reply.json()["outcome"] == "exhausted" for reply in replies)
        assert task.status == story.status == "waiting_human_review"
        assert task.current_iteration == 0
        assert "Durable refusal" in story.quarantine_reason["detail"]
        assert "Stale callback" not in story.quarantine_reason["detail"]
        assert (
            story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
        )
    else:
        assert sum(reply.json()["outcome"] == "retried" for reply in replies) == 1
        assert (task.status, task.current_iteration) == ("todo", 1)
        assert story.status == "in_progress" and story.owner_notification is None
    before = (
        task.status,
        task.current_iteration,
        story.quarantine_reason,
        story.owner_notification,
    )
    await async_client.post(url, json=command)
    await db_session.refresh(task)
    await db_session.refresh(story)
    assert (
        task.status,
        task.current_iteration,
        story.quarantine_reason,
        story.owner_notification,
    ) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["admission", "iteration", "result", "unrelated"])
async def test_malformed_or_unrelated_terminal_evidence_refuses_without_mutation(
    async_client, db_session, dirty_story, corruption
):
    from sqlalchemy import select

    from shared.models import TaskEvent

    sid, command = await failed_attempt(async_client, db_session, dirty_story, gave_up=True)
    run = await db_session.get(Run, command["attempt_id"], populate_existing=True)
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    if corruption == "admission":
        event = await db_session.scalar(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
        )
        event.details = {"pr_conflict_repair": {"broken": True}}
    elif corruption == "iteration":
        run.run_metadata = {**run.run_metadata, "iteration": True}
    elif corruption == "result":
        run.result = {"engineering_status": "not-an-outcome"}
    else:
        run.task_id = dirty_story[3]
    await db_session.commit()
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome",
        json=command,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "pr_conflict_repair_refused"
    await db_session.refresh(task)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert (task.status, task.current_iteration) == ("in_dev", 0)
    assert story.status == "in_progress" and story.owner_notification is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["infrastructure", "resource", "live", "unproven_backlog", "empty"]
)
async def test_scoped_retry_retains_native_refusal_and_empty_precedence(
    async_client, db_session, dirty_story, boundary
):
    sid, command = await failed_attempt(
        async_client, db_session, dirty_story, ceiling=boundary == "empty"
    )
    run = await db_session.get(Run, command["attempt_id"], populate_existing=True)
    task = await db_session.get(Task, command["task_id"], populate_existing=True)
    if boundary == "infrastructure":
        run.result = {
            "engineering_status": "failed",
            "execution": {
                "execution_phase": "pre_agent_refused",
                "infrastructure_refusal": "project_locked",
            },
        }
    elif boundary == "resource":
        run.result = {
            "engineering_status": "failed",
            "allocation_failure_reason": "impossible_capacity",
        }
    elif boundary == "live":
        db_session.add(
            Run(
                id=command["attempt_id"] + "-live",
                project_id=task.project_id,
                task_id=task.id,
                story_id=sid,
                type="engineering",
                status="running",
            )
        )
    elif boundary == "unproven_backlog":
        task.status = "backlog"
    else:
        run.result = {"engineering_status": "failed", "failure_reason": "no_new_commit"}
    await db_session.commit()
    before = (task.status, task.current_iteration)
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=command
    )
    if boundary == "empty":
        assert response.status_code == 200 and response.json()["outcome"] == "exhausted", (
            response.text
        )
        story = await db_session.get(Story, sid, populate_existing=True)
        assert story.quarantine_reason["code"] == "no_new_commit"
        assert (
            story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
        )
    else:
        assert response.status_code == (200 if boundary == "live" else 409), response.text
        await db_session.refresh(task)
        assert (task.status, task.current_iteration) == before
