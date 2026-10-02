"""Real paid decisions, atomic no-Run stops and deliberate native recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
import os

import jwt
import pytest
from sqlalchemy import select
from test_pr_conflict_attempt import failed_attempt
from test_pr_conflict_repair import dirty_story as _dirty_story

from shared.models import (
    EngineeringBudgetReservation,
    Project,
    Run,
    Story,
    Task,
    TaskEvent,
    WorkAdmissionAudit,
)

dirty_story = _dirty_story
DISPATCH = "/api/work-admission/engineering-dispatches"


def bearer(user_id):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=1)},
        os.environ["LK_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"X-Internal-Key": "", "Authorization": f"Bearer {token}"}


async def ready_task(client, db, fixture, later):
    sid, command, _, _, _ = fixture
    if later:
        _, outcome = await failed_attempt(client, db, fixture)
        result = await client.post(
            f"/api/stories/{sid}/repair-pr-conflicts/attempt-outcome", json=outcome
        )
        assert result.json()["outcome"] == "retried", result.text
        tid = outcome["task_id"]
    else:
        admitted = await client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
        assert admitted.status_code == 200, admitted.text
        tid = admitted.json()["task_id"]
    task = await db.get(Task, tid, populate_existing=True)
    project = await db.get(Project, task.project_id)
    project.config = {**project.config, "workspace_ready": True}
    await db.commit()
    policy_url = f"/api/engineering-budget-policies/{project.owner_id}"
    policy = await client.put(
        policy_url,
        json={"limit_microusd": 0, "attempt_reservation_microusd": 10, "state": "enabled"},
    )
    assert policy.status_code in {200, 201}, policy.text
    return sid, tid, policy_url, policy.json()["version"]


async def history(db, tid):
    runs = (
        await db.scalars(
            select(Run)
            .where(Run.task_id == tid)
            .order_by(Run.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    events = (
        await db.scalars(select(TaskEvent).where(TaskEvent.task_id == tid).order_by(TaskEvent.id))
    ).all()
    return [(r.id, r.status, r.run_metadata, r.result) for r in runs], [
        (e.id, e.details) for e in events
    ]


async def decisions(db, tid):
    audits = (
        await db.scalars(
            select(WorkAdmissionAudit)
            .where(WorkAdmissionAudit.subject == "paid_work")
            .order_by(WorkAdmissionAudit.id)
        )
    ).all()
    return [
        a
        for a in audits
        if (a.command_payload or {}).get("task_id") == tid
        and a.reason == "engineering_budget_denied"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True], ids=["iteration0", "bounded-retry"])
async def test_budget_refusal_is_atomic_once_and_the_repair_command_resumes_the_unspent_attempt(
    async_client, db_session, dirty_story, later
):
    sid, tid, policy_url, version = await ready_task(async_client, db_session, dirty_story, later)
    before_runs, before_events = await history(db_session, tid)
    original_bound = (await db_session.get(Task, tid)).max_iterations
    # Discard the deciding response. Concurrent native repeats see the stopped
    # Story and cannot buy another decision or overwrite its notices.
    await async_client.post(DISPATCH, json={"task_id": tid})
    replies = await asyncio.gather(
        *[async_client.post(DISPATCH, json={"task_id": tid}) for _ in range(5)]
    )
    assert all(r.json()["reason"] == "task_not_dispatchable" for r in replies)
    task = await db_session.get(Task, tid, populate_existing=True)
    story = await db_session.get(Story, sid, populate_existing=True)
    # No Run was bought, so the Task keeps its unspent attempt; only the Story stops.
    assert (task.status, story.status) == ("todo", "waiting_human_review")
    assert task.current_iteration == int(later)
    assert task.max_iterations == original_bound
    assert story.quarantine_reason["code"] == "engineering_budget_denied"
    assert "No budget: limit $0.00" in story.quarantine_reason["detail"]
    assert story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
    owed = story.owner_notification
    audits = await decisions(db_session, tid)
    assert len(audits) == 1
    did = audits[0].reference_id
    assert tid in story.quarantine_reason["detail"] and did in story.quarantine_reason["detail"]
    assert audits[0].outcome == "denied" and audits[0].message
    assert await db_session.get(Run, did) is None
    reservation = await db_session.scalar(
        select(EngineeringBudgetReservation).where(EngineeringBudgetReservation.attempt_id == did)
    )
    assert (
        reservation.outcome == "denied"
        and reservation.state is None
        and reservation.active_held_microusd == 0
    )
    after_runs, after_events = await history(db_session, tid)
    assert after_runs == before_runs and after_events[: len(before_events)] == before_events
    # One immutable note records the decision; it adds no status edge.
    (note,) = after_events[len(before_events) :]
    assert note[1]["engineering_dispatch_refusal"] == {
        "task_id": tid,
        "decision_id": did,
        "reason": "engineering_budget_denied",
    }
    # No operator start path walks past the stopped Story.
    assert (await async_client.post(f"/api/tasks/{tid}/spawn-worker")).status_code == 409
    assert (await async_client.post(f"/api/tasks/{tid}/start")).status_code == 409
    # Restore real policy capacity through its existing versioned API.
    restored = await async_client.put(
        policy_url,
        json={
            "limit_microusd": 100,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": version,
        },
    )
    assert restored.status_code == 200, restored.text
    # Funding by itself does not restart the stopped Story.
    assert (await async_client.post(DISPATCH, json={"task_id": tid})).json()[
        "reason"
    ] == "task_not_dispatchable"
    # The ordinary repair command is admissible again; a lost response repeats.
    for _ in range(2):
        repaired = await async_client.post(
            f"/api/stories/{sid}/repair-pr-conflicts", json=dirty_story[1]
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["outcome"] == "reused" and repaired.json()["task_id"] == tid
        assert repaired.json()["max_iterations"] == original_bound
    await db_session.refresh(story)
    assert (story.status, story.quarantine_reason) == ("in_progress", None)
    assert story.owner_notification == owed
    # The command wrote no Task edge: the Task never left todo.
    assert (await history(db_session, tid))[1] == after_events
    admitted = await async_client.post(DISPATCH, json={"task_id": tid})
    assert admitted.json()["outcome"] == "admitted", admitted.text
    rid = admitted.json()["run_id"]
    run = await db_session.get(Run, rid)
    assert run.run_metadata["iteration"] == int(later) and rid != did
    assert (await async_client.post(DISPATCH, json={"task_id": tid})).json()["run_id"] == rid
    assert [
        r for r in (await history(db_session, tid))[0] if r[0] in {old[0] for old in before_runs}
    ] == before_runs
    assert [a.reference_id for a in await decisions(db_session, tid)] == [did]


@pytest.mark.asyncio
async def test_interruption_before_commit_rolls_back_both_rows_audit_and_budget_decision(
    async_client, db_session, dirty_story, monkeypatch
):
    from src import engineering_dispatch_admission as admission

    sid, tid, _, _ = await ready_task(async_client, db_session, dirty_story, False)
    before = await history(db_session, tid)
    dispose = admission._dispose_conflict_refusal

    async def interrupt(*args):
        await dispose(*args)
        raise RuntimeError("interrupted before admission commit")

    with monkeypatch.context() as scope:
        scope.setattr(admission, "_dispose_conflict_refusal", interrupt)
        with pytest.raises(RuntimeError, match="before admission commit"):
            await async_client.post(DISPATCH, json={"task_id": tid})
    await db_session.rollback()
    task = await db_session.get(Task, tid, populate_existing=True)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert (task.status, story.status, story.owner_notification) == ("todo", "in_progress", None)
    assert await decisions(db_session, tid) == []
    assert (
        await db_session.scalars(
            select(EngineeringBudgetReservation).where(EngineeringBudgetReservation.task_id == tid)
        )
    ).all() == []
    assert await history(db_session, tid) == before
    response = await async_client.post(DISPATCH, json={"task_id": tid})
    assert response.json()["refusal_disposition"]["task_id"] == tid, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pr", "cycle", "live", "unrelated_stop", "malformed"])
async def test_current_work_and_admission_outrank_old_discovery(
    async_client, db_session, dirty_story, change
):
    sid, tid, _, _ = await ready_task(async_client, db_session, dirty_story, False)
    if change == "live":
        # Restore capacity, admit a real live Run, then tighten policy again.
        project = await db_session.get(Project, dirty_story[1]["project_id"])
        url = f"/api/engineering-budget-policies/{project.owner_id}"
        policy = (await async_client.get(url)).json()["policy"]
        await async_client.put(
            url,
            json={
                "limit_microusd": 100,
                "attempt_reservation_microusd": 10,
                "state": "enabled",
                "version": policy["version"],
            },
        )
        result = await async_client.post(DISPATCH, json={"task_id": tid})
        assert result.json()["outcome"] == "admitted", result.text
    elif change == "malformed":
        await async_client.post(
            f"/api/tasks/{tid}/events",
            json={"event_type": "note", "details": {"pr_conflict_repair": {"malformed": True}}},
        )
    else:
        story = await db_session.get(Story, sid, populate_existing=True)
        if change == "pr":
            story.pr_number += 1
        elif change == "cycle":
            story.reopened_at = story.created_at + timedelta(seconds=1)
        else:
            stopped = await async_client.post(
                f"/api/stories/{sid}/human-review", json={"actor": "test"}
            )
            assert stopped.status_code == 200
        await db_session.commit()
    before = await history(db_session, tid)
    response = await async_client.post(DISPATCH, json={"task_id": tid})
    assert response.status_code == (409 if change == "malformed" else 200), response.text
    assert response.json().get("refusal_disposition") is None
    assert await decisions(db_session, tid) == []
    assert await history(db_session, tid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["pr", "cycle", "foreign_story", "unsettled_sibling", "live_work"]
)
async def test_repair_command_cannot_resume_replaced_or_busy_work(
    async_client, db_session, dirty_story, change
):
    sid, tid, policy_url, version = await ready_task(async_client, db_session, dirty_story, False)
    stopped = await async_client.post(DISPATCH, json={"task_id": tid})
    assert stopped.json()["refusal_disposition"], stopped.text
    notice = (await async_client.get(f"/api/stories/{sid}/owner-notification")).json()
    restored = await async_client.put(
        policy_url,
        json={
            "limit_microusd": 100,
            "attempt_reservation_microusd": 10,
            "state": "enabled",
            "version": version,
        },
    )
    assert restored.status_code == 200
    if change == "pr":
        changed = await async_client.patch(f"/api/stories/{sid}", json={"pr_number": 4})
        assert changed.status_code == 200
    elif change == "cycle":
        story = await db_session.get(Story, sid, populate_existing=True)
        story.reopened_at = story.created_at + timedelta(seconds=1)
        await db_session.commit()
    elif change == "foreign_story":
        other = await async_client.post(
            "/api/stories/", json={"project_id": dirty_story[1]["project_id"], "title": "Unrelated"}
        )
        changed = await async_client.patch(
            f"/api/tasks/{tid}", json={"story_id": other.json()["id"]}
        )
        assert changed.status_code == 200
    else:
        other = await async_client.post(
            "/api/tasks/",
            json={
                "project_id": dirty_story[1]["project_id"],
                "story_id": sid,
                "title": "Newer work",
                "status": "todo",
            },
        )
        assert other.status_code in {200, 201}, other.text
        if change == "live_work":
            admitted = await async_client.post(
                DISPATCH,
                json={
                    "task_id": other.json()["id"],
                    "origin": "admin",
                    "overrides": ["story_waiting_human_review"],
                },
            )
            assert admitted.json()["outcome"] == "admitted", admitted.text
    before = await history(db_session, tid)
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts", json=dirty_story[1]
    )
    assert response.status_code == 409, response.text
    assert await history(db_session, tid) == before
    task = await db_session.get(Task, tid, populate_existing=True)
    assert (task.status, task.current_iteration) == ("todo", 0)
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "waiting_human_review"
    assert (await async_client.get(f"/api/stories/{sid}/owner-notification")).json() == notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "emergency_stop",
        "paid_work_limit",
        "executor_unavailable",
        "executor_confirmation_required",
        "workspace_ensure_failed",
    ],
)
async def test_other_native_no_run_refusals_preserve_their_routing_and_budget_priority(
    async_client, db_session, dirty_story, redis_client, reason
):
    from test_story_infrastructure_park import _publish_executors

    from shared.contracts.dto.executor_diagnostics import ExecutorAvailability

    sid, tid, _, _ = await ready_task(async_client, db_session, dirty_story, False)
    controls_url = "/api/work-admission/controls"
    controls = (await async_client.get(controls_url)).json()
    modified = {
        **controls,
        **(
            {"emergency_stop": True}
            if reason == "emergency_stop"
            else {"max_concurrent_paid_runs": 0}
            if reason == "paid_work_limit"
            else {}
        ),
    }
    try:
        changed = await async_client.put(controls_url, json=modified)
        assert changed.status_code == 200, changed.text
        if reason.startswith("executor_"):
            await _publish_executors(
                redis_client,
                ExecutorAvailability.UNAVAILABLE
                if reason == "executor_unavailable"
                else ExecutorAvailability.UNKNOWN,
                "local_auth_invalid"
                if reason == "executor_unavailable"
                else "profile_metadata_unverifiable",
            )
        elif reason == "workspace_ensure_failed":
            task = await db_session.get(Task, tid)
            project = await db_session.get(Project, task.project_id)
            project.config = {"scaffold_error": "Synthetic failed workspace ensure"}
            await db_session.commit()
        response = await async_client.post(DISPATCH, json={"task_id": tid})
        assert response.json()["reason"] == reason, response.text
        task = await db_session.get(Task, tid, populate_existing=True)
        story = await db_session.get(Story, sid, populate_existing=True)
        infrastructure = reason.startswith("executor_") or reason == "workspace_ensure_failed"
        # An infrastructure park parks the Task; a paid refusal leaves it unspent.
        assert task.status == ("waiting_human_review" if infrastructure else "todo")
        assert story.status == "waiting_human_review"
        if not infrastructure:
            assert story.quarantine_reason["code"] == "engineering_dispatch_refused"
        assert (
            task.current_iteration == 0
            and story.owner_notification["state"]
            == story.owner_notification["admin_state"]
            == "owed"
        )
        assert response.json()["infrastructure_park"] == (
            "parked"
            if reason.startswith("executor_") or reason == "workspace_ensure_failed"
            else None
        )
        assert bool(response.json()["refusal_disposition"]) == (
            reason in {"emergency_stop", "paid_work_limit"}
        )
        assert not (await db_session.scalars(select(Run).where(Run.task_id == tid))).all()
        assert not (
            await db_session.scalars(
                select(EngineeringBudgetReservation).where(
                    EngineeringBudgetReservation.task_id == tid
                )
            )
        ).all()
        assert await decisions(db_session, tid) == []
    finally:
        await async_client.put(controls_url, json=controls)
        await _publish_executors(redis_client, ExecutorAvailability.AVAILABLE, "ready")
