"""Atomic dirty-PR admission and released-state recovery on real PostgreSQL."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select

from shared.models import Project, Repository, Run, Story, Task, TaskEvent


@pytest.fixture
async def dirty_story(async_client, db_session, monkeypatch):
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await async_client.post("/api/users/", json={"telegram_id": telegram_id})
    project = await async_client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "title": "Dirty conflict fixture",
            "initiating_run_id": "fixture-init",
            "status": "active",
            "config": {},
        },
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    created = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Dirty PR"}
    )
    assert created.status_code == 201, created.text
    story = created.json()
    sid = story["id"]
    await async_client.post(f"/api/stories/{sid}/start")
    await async_client.post(f"/api/stories/{sid}/pr_review")
    row = await db_session.get(Story, sid)
    row.pr_number = 3
    repo_id = f"repo-{uuid.uuid4().hex}"
    db_session.add(
        Repository(
            id=repo_id,
            project_id=uuid.UUID(project_id),
            name="product",
            git_url="https://github.com/synthetic/product",
            role="primary",
        )
    )
    original = Task(
        id=f"original-{uuid.uuid4().hex}",
        project_id=uuid.UUID(project_id),
        story_id=sid,
        title="Original work",
        status="done",
        type="feature",
    )
    db_session.add(original)
    await db_session.commit()
    pr = {
        "number": 3,
        "state": "open",
        "merged_at": None,
        "mergeable_state": "dirty",
        "head": {
            "sha": "a" * 40,
            "ref": f"story/{sid}",
            "repo": {"full_name": "synthetic/product"},
        },
        "base": {"sha": "b" * 40, "ref": "trunk", "repo": {"full_name": "synthetic/product"}},
    }
    github = AsyncMock()
    github.__aenter__.return_value = github
    github.get_pull_request.return_value = pr
    github.get_repo.return_value = SimpleNamespace(default_branch="trunk")
    github.get_ref_sha.side_effect = lambda owner, repo, ref: (
        "b" * 40 if ref == "heads/trunk" else pr["head"]["sha"]
    )
    import src.routers._story_actions as actions

    # This is the API's GitHub read boundary, never a live repository.
    monkeypatch.setattr(actions, "PR_CONFLICT_GITHUB", lambda: github, raising=False)
    command = {
        "project_id": project_id,
        "pr_number": 3,
        "cycle_started_at": story["created_at"],
        "expected_head_sha": "a" * 40,
    }
    return sid, command, pr, original.id, github


@pytest.mark.asyncio
async def test_concurrent_admission_is_one_atomic_task_and_normal_dispatch(
    async_client, db_session, dirty_story
):
    sid, command, _, original, _ = dirty_story
    url = f"/api/stories/{sid}/repair-pr-conflicts"
    responses = await asyncio.gather(*[async_client.post(url, json=command) for _ in range(6)])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    bodies = [r.json() for r in responses]
    assert sum(b["outcome"] == "admitted" for b in bodies) == 1
    tid = bodies[0]["task_id"]
    assert {b["task_id"] for b in bodies} == {tid}
    task = await db_session.get(Task, tid)
    assert task.type == "fix" and task.status == "todo"
    assert task.story_id == sid and task.max_iterations > 0
    assert "a" * 40 in task.description and "trunk" in task.description
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "in_progress" and story.waiting_on == "none"
    assert story.reopened_at is None
    assert (await db_session.get(Task, original)).status == "done"
    events = (await db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == tid))).all()
    assert len(events) == 1
    admitted = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": tid}
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["reason"] == "workspace_not_ready", admitted.text
    project = await db_session.get(Project, task.project_id)
    project.config = {**project.config, "workspace_ready": True}
    await db_session.commit()
    admitted = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": tid}
    )
    assert admitted.json()["outcome"] == "admitted", admitted.text
    run = await db_session.get(Run, admitted.json()["run_id"])
    assert run.task_id == tid and run.story_id == sid
    again = await async_client.post(url, json=command)
    assert again.status_code == 200 and again.json()["task_id"] == tid


@pytest.mark.asyncio
async def test_released_dirty_quarantine_recovers_without_new_cycle(
    async_client, db_session, dirty_story
):
    sid, command, _, original, _ = dirty_story
    await async_client.patch(
        f"/api/stories/{sid}",
        json={
            "quarantine_reason": {
                "reason": "github_app_merge_refused",
                "mergeable_state": "dirty",
                "pr_number": 3,
            }
        },
    )
    await async_client.post(f"/api/stories/{sid}/human-review")
    command.pop("expected_head_sha")
    repaired = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["outcome"] == "admitted"
    assert (await db_session.get(Task, original)).status == "done"
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "in_progress" and story.reopened_at is None
    assert story.pr_number == 3


def started_run(db, task, status="failed"):
    """A repair Run of the Task's iteration that a consumer took up: work began."""
    db.add(
        Run(
            id=f"eng-{uuid.uuid4().hex[:12]}",
            type="engineering",
            status=status,
            project_id=task.project_id,
            story_id=task.story_id,
            task_id=task.id,
            run_metadata={"iteration": task.current_iteration},
            started_at=datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_ci_retry_keeps_conflict_repair_cycle_and_bound(
    async_client, db_session, dirty_story
):
    sid, command, _, _, _ = dirty_story
    repair = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert repair.status_code == 200, repair.text
    task = await db_session.get(Task, repair.json()["task_id"])
    task.status = "done"
    started_run(db_session, task, "completed")
    await db_session.commit()
    await async_client.post(f"/api/stories/{sid}/pr_review")
    retry = await async_client.post(f"/api/stories/{sid}/retry-after-ci-failure")
    assert retry.status_code == 200, retry.text
    assert retry.json()["reopened_at"] is None
    exhausted = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert exhausted.status_code == 200, exhausted.text
    assert exhausted.json()["outcome"] == "exhausted"
    assert exhausted.json()["task_id"] == task.id


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["done", "failed", "cancelled", "waiting_human_review"])
async def test_terminal_repair_exhausts_once_even_after_head_changes(
    async_client, db_session, dirty_story, ending
):
    sid, command, pr, _, _ = dirty_story
    url = f"/api/stories/{sid}/repair-pr-conflicts"
    first = await async_client.post(url, json=command)
    assert first.status_code == 200, first.text
    tid = first.json()["task_id"]
    task = await db_session.get(Task, tid)
    task.status = ending
    if ending == "failed":
        task.current_iteration = task.max_iterations
    # Ordinary failure settlement may replace failure_metadata; identity survives.
    task.failure_metadata = {"last_failure": "synthetic failure"}
    # Exhaustion counts started repair work, so the ended Task carries its Run.
    started_run(db_session, task, "completed" if ending == "done" else "failed")
    await db_session.commit()
    pr["head"]["sha"] = "c" * 40
    command["expected_head_sha"] = "c" * 40
    exhausted = await async_client.post(url, json=command)
    assert exhausted.status_code == 200, exhausted.text
    assert exhausted.json()["outcome"] == "exhausted"
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "waiting_human_review"
    assert tid in story.quarantine_reason["detail"]
    assert "PR #3" in story.owner_notification["text"]
    assert story.owner_notification["state"] == "owed"
    assert story.owner_notification["admin_state"] == "owed"
    before = story.owner_notification
    again = await async_client.post(url, json=command)
    assert again.status_code == 200 and again.json()["outcome"] == "exhausted"
    await db_session.refresh(story)
    assert story.owner_notification == before
    assert len((await db_session.scalars(select(Task).where(Task.story_id == sid))).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "waiting_human_review"])
async def test_a_repair_task_that_ended_before_any_started_run_is_not_exhausted(
    async_client, db_session, dirty_story, ending
):
    sid, command, _, _, _ = dirty_story
    url = f"/api/stories/{sid}/repair-pr-conflicts"
    first = await async_client.post(url, json=command)
    task = await db_session.get(Task, first.json()["task_id"])
    task.status = ending
    await db_session.commit()
    refused = await async_client.post(url, json=command)
    assert refused.status_code == 409, refused.text
    assert "before any repair Run started" in refused.json()["detail"]["message"]
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "in_progress" and story.quarantine_reason is None
    assert story.owner_notification is None


@pytest.mark.asyncio
async def test_failed_repair_keeps_its_normal_retry_budget(async_client, db_session, dirty_story):
    sid, command, _, _, _ = dirty_story
    url = f"/api/stories/{sid}/repair-pr-conflicts"
    first = await async_client.post(url, json=command)
    task = await db_session.get(Task, first.json()["task_id"])
    task.status = "failed"
    await db_session.commit()
    retrying = await async_client.post(url, json=command)
    assert retrying.status_code == 200, retrying.text
    assert retrying.json()["outcome"] == "reused"
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "in_progress" and story.owner_notification is None


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize(
    "mismatch",
    [
        "project",
        "cycle",
        "pr",
        "head",
        "branch",
        "default",
        "quarantine",
        "live",
        "foreign_owner",
        "github_failure",
    ],
)
async def test_unproven_or_unauthorized_repair_writes_nothing(
    async_client, db_session, dirty_story, mismatch, released
):
    sid, command, pr, original, github = dirty_story
    if released:
        await async_client.patch(
            f"/api/stories/{sid}",
            json={
                "quarantine_reason": {
                    "reason": "github_app_merge_refused",
                    "mergeable_state": "dirty",
                    "pr_number": 3,
                }
            },
        )
        await async_client.post(f"/api/stories/{sid}/human-review")
    headers = {}
    expected = 409
    if mismatch == "project":
        command["project_id"] = str(uuid.uuid4())
    elif mismatch == "cycle":
        command["cycle_started_at"] = "2020-01-01T00:00:00Z"
    elif mismatch == "pr":
        command["pr_number"] = 4
    elif mismatch == "head":
        command["expected_head_sha"] = "f" * 40
    elif mismatch == "branch":
        pr["head"]["ref"] = "story/another"
    elif mismatch == "default":
        pr["base"]["ref"] = "obsolete"
    elif mismatch == "quarantine":
        await async_client.patch(
            f"/api/stories/{sid}", json={"quarantine_reason": {"reason": "unrelated_review"}}
        )
        if not released:
            await async_client.post(f"/api/stories/{sid}/human-review")
    elif mismatch == "live":
        db_session.add(
            Run(
                id=f"live-{uuid.uuid4().hex}",
                task_id=original,
                story_id=sid,
                type="engineering",
                status="running",
            )
        )
        await db_session.commit()
    elif mismatch == "foreign_owner":
        telegram_id = uuid.uuid4().int % 1_000_000_000
        await async_client.post("/api/users/", json={"telegram_id": telegram_id})
        headers["X-Telegram-ID"] = str(telegram_id)
        expected = 403
    else:
        github.get_pull_request.side_effect = RuntimeError("synthetic GitHub unavailable")
        expected = 503
    story = await db_session.get(Story, sid, populate_existing=True)
    before = (story.status, story.reopened_at, story.quarantine_reason, story.owner_notification)
    response = await async_client.post(
        f"/api/stories/{sid}/repair-pr-conflicts", json=command, headers=headers
    )
    assert response.status_code == expected, response.text
    await db_session.refresh(story)
    assert (
        story.status,
        story.reopened_at,
        story.quarantine_reason,
        story.owner_notification,
    ) == before
    assert len((await db_session.scalars(select(Task).where(Task.story_id == sid))).all()) == 1


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_task_and_story_then_retry_recovers(
    async_client, db_session, dirty_story, monkeypatch
):
    sid, command, _, _, _ = dirty_story

    from src.database import async_session_maker, get_async_session
    from src.main import app

    async def broken_session():
        async with async_session_maker() as session:

            async def refuse():
                raise RuntimeError("synthetic interrupted commit")

            monkeypatch.setattr(session, "commit", refuse)
            yield session

    app.dependency_overrides[get_async_session] = broken_session
    try:
        with pytest.raises(RuntimeError, match="interrupted commit"):
            await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    finally:
        app.dependency_overrides.pop(get_async_session)
    assert len((await db_session.scalars(select(Task).where(Task.story_id == sid))).all()) == 1
    story = await db_session.get(Story, sid, populate_existing=True)
    assert story.status == "pr_review"
    retry = await async_client.post(f"/api/stories/{sid}/repair-pr-conflicts", json=command)
    assert retry.status_code == 200 and retry.json()["outcome"] == "admitted"
