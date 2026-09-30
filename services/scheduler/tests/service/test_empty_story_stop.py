"""Empty engineering stops leave completion and owe durable notices through the real API."""

import json
import os
from unittest.mock import AsyncMock, patch
import uuid

from _owner_notification_clock import age_last_attempt_by_one_interval
import asyncpg
import pytest
from structlog.testing import capture_logs

from shared.clients.github import NoCommitsBetweenError
from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.po import POSystemEvent, from_flat_fields, unprotect_po_payload
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.story_completion import complete_stories
from src.tasks.supervisor.liveness import supervise_failed_tasks


@pytest.fixture
async def completion_story(api_client):
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await api_client.request(
        "POST",
        "users/",
        json={
            "telegram_id": telegram_id,
            "username": f"empty_{telegram_id}",
        },
    )
    project = (
        await api_client.request(
            "POST",
            "projects/",
            json={
                "title": "Empty story stop",
                "initiating_run_id": "empty-fixture",
                "config": {},
            },
            headers={"X-Telegram-ID": str(telegram_id)},
        )
    ).json()
    project_id = project["id"]
    await api_client.request(
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"empty-{uuid.uuid4().hex[:8]}",
            "git_url": "https://github.com/fixture/empty.git",
        },
    )
    story = (
        await api_client.request(
            "POST",
            "stories/",
            json={
                "project_id": project_id,
                "title": "No commits",
            },
        )
    ).json()
    story_id = story["id"]
    await api_client.transition_story(story_id, "start")
    task = (
        await api_client.request(
            "POST",
            "tasks/",
            json={
                "project_id": project_id,
                "story_id": story_id,
                "title": "Finished task",
                "status": "done",
            },
        )
    ).json()
    yield story_id, project_id, task["id"], telegram_id
    await api_client.request("DELETE", f"projects/{project_id}")


async def _read_row(story_id):
    db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        row = dict(
            await db.fetchrow(
                "SELECT status, waiting_on, quarantine_reason, owner_notification "
                "FROM stories WHERE id=$1",
                story_id,
            )
        )
        for key in ("quarantine_reason", "owner_notification"):
            if row[key] is not None:
                row[key] = json.loads(row[key])
        return row
    finally:
        await db.close()


async def _exhausted_task(api, project_id, story_id, *, empty=True, priority=0):
    task = (
        await api.request(
            "POST",
            "tasks/",
            json={
                "project_id": project_id,
                "story_id": story_id,
                "title": "Exhausted attempt",
                "status": "failed",
                "max_iterations": 3,
                "priority": priority,
            },
        )
    ).json()
    await api.update_task(task["id"], {"current_iteration": 3})
    result = {"engineering_status": "failed"}
    if empty:
        result["failure_reason"] = "no_new_commit"
    db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        await db.execute(
            "INSERT INTO runs (id, type, status, project_id, story_id, task_id, "
            "metadata, result, created_at, completed_at) VALUES "
            "($1, 'engineering', 'failed', $2, $3, $4, '{}'::json, $5::json, now(), now())",
            "exhausted-" + uuid.uuid4().hex,
            uuid.UUID(project_id),
            story_id,
            task["id"],
            json.dumps(result),
        )
    finally:
        await db.close()
    return task["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_first", [False, True])
@pytest.mark.parametrize("interrupt_task", [False, True])
async def test_exhausted_siblings_keep_the_empty_cause_across_order_and_partial_settlement(
    api_client,
    completion_story,
    empty_first,
    interrupt_task,
    capsys,
):
    story_id, project_id, _, _ = completion_story
    ordinary = await _exhausted_task(
        api_client,
        project_id,
        story_id,
        empty=False,
        priority=int(empty_first),
    )
    empty = await _exhausted_task(
        api_client,
        project_id,
        story_id,
        priority=int(not empty_first),
    )
    selected = [task.id for task in await api_client.get_tasks_by_status("failed")]
    assert (selected.index(empty) < selected.index(ordinary)) is empty_first
    original = api_client.transition_task

    async def interrupt(task_id, *args, **kwargs):
        if interrupt_task and task_id == empty:
            raise ConnectionError("Authorization: Bearer TASK_WRITE_CANARY")
        return await original(task_id, *args, **kwargs)

    with patch.object(api_client, "transition_task", side_effect=interrupt):
        await supervise_failed_tasks(api_client, AsyncMock())
    row = await _read_row(story_id)
    assert row["status"] == "waiting_human_review"
    assert row["waiting_on"] == "human_review"
    assert row["quarantine_reason"]["code"] == "no_new_commit"
    notice = OwnerNotification.model_validate(row["owner_notification"])
    assert notice.state is OwnerNotificationState.OWED
    assert notice.admin_state is OwnerNotificationState.OWED
    assert empty in row["quarantine_reason"]["detail"]
    assert (await api_client.get_task(ordinary)).status.value == "waiting_human_review"
    if interrupt_task:
        assert (await api_client.get_task(empty)).status.value == "failed"
    await supervise_failed_tasks(api_client, AsyncMock())
    assert (await api_client.get_task(empty)).status.value == "waiting_human_review"
    assert await _read_row(story_id) == row
    assert "TASK_WRITE_CANARY" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated", ["cause", "notice", "missing_notice", "bare"])
async def test_empty_exhaustion_does_not_accept_an_unrelated_stop(
    api_client,
    completion_story,
    unrelated,
):
    from shared.contracts.dto.story_failure import StoryFailure

    story_id, project_id, _, _ = completion_story
    task_id = await _exhausted_task(api_client, project_id, story_id)
    run = (await api_client.list_runs(task_id=task_id, run_type="engineering"))[0]
    detail = (
        "another attempt"
        if unrelated == "cause"
        else f"Task {task_id} exhausted its 3 retries. "
        f"Attempt {run.id} produced no new commit to merge or deploy."
    )
    if unrelated == "bare":
        await api_client.transition_story(story_id, "human-review")
    else:
        await api_client.stop_story(
            story_id,
            "human-review",
            StoryFailure(code="no_new_commit", source="scheduler", detail=detail),
            actor="fixture",
        )
    if unrelated == "notice":
        notice = await api_client.get_story_owner_notification(story_id)
        notice.admin_text = "Notice from a different stop"
        await api_client.update_story_owner_notification(story_id, notice.model_dump(mode="json"))
    if unrelated == "missing_notice":
        db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        try:
            await db.execute("UPDATE stories SET owner_notification=NULL WHERE id=$1", story_id)
        finally:
            await db.close()
    row = await _read_row(story_id)
    assert await supervise_failed_tasks(api_client, AsyncMock()) == {"retried": 0, "escalated": 0}
    assert (await api_client.get_task(task_id)).status.value == "failed"
    assert await _read_row(story_id) == row


def _github(*, empty=True):
    github = AsyncMock()
    github.__aenter__.return_value = github
    github.get_ref_sha.return_value = "a" * 40
    if empty:
        github.create_pull_request.side_effect = NoCommitsBetweenError(
            "No commits between main and story. Authorization: Bearer PR_DETAIL_CANARY"
        )
    else:

        async def create(*args, head, **kwargs):
            return {"number": 7, "head": {"ref": head, "sha": "a" * 40}}

        github.create_pull_request.side_effect = create
    return github


@pytest.mark.asyncio
async def test_remaining_empty_sibling_recognizes_the_settled_siblings_stop(
    api_client,
    completion_story,
):
    story_id, project_id, _, _ = completion_story
    siblings = [
        await _exhausted_task(api_client, project_id, story_id, priority=priority)
        for priority in (0, 1)
    ]
    original = api_client.transition_task
    interrupted = []

    async def fail_remaining_sibling(task_id, *args, **kwargs):
        story = await api_client.get_story(story_id)
        if task_id not in story.quarantine_reason["detail"]:
            interrupted.append(task_id)
            raise ConnectionError("sibling task transition unavailable")
        return await original(task_id, *args, **kwargs)

    with patch.object(api_client, "transition_task", side_effect=fail_remaining_sibling):
        await supervise_failed_tasks(api_client, AsyncMock())
    assert len(interrupted) == 1
    row = await _read_row(story_id)
    assert row["owner_notification"]["state"] == "owed"
    assert row["owner_notification"]["admin_state"] == "owed"
    with patch.object(api_client, "stop_story", wraps=api_client.stop_story) as stops:
        await supervise_failed_tasks(api_client, AsyncMock())
    stops.assert_not_awaited()
    for task_id in siblings:
        task = await api_client.get_task(task_id)
        assert task.status.value == "waiting_human_review"
        assert (task.current_iteration, task.max_iterations) == (3, 3)
    assert await _read_row(story_id) == row


@pytest.mark.asyncio
async def test_empty_branch_stop_is_atomic_and_the_next_cycle_makes_no_pr_attempt(
    api_client,
    completion_story,
):
    story_id, _, _, _ = completion_story
    github = _github()
    stream = RedisStreamClient(os.environ["REDIS_URL"])
    await stream.connect()
    try:
        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=github),
            patch.object(api_client, "update_story", wraps=api_client.update_story) as writes,
            capture_logs() as logs,
        ):
            assert await complete_stories(api_client, stream) == 0
            row = await _read_row(story_id)
            assert row["status"] == "waiting_human_review"
            assert row["waiting_on"] == "human_review"
            assert row["quarantine_reason"]["code"] == "no_new_commit"
            notice = OwnerNotification.model_validate(row["owner_notification"])
            assert notice.state is OwnerNotificationState.OWED
            assert notice.admin_state is OwnerNotificationState.OWED
            assert "nothing was produced" in notice.text
            assert "a person" in notice.text
            assert story_id not in {
                s.id for s in await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
            }
            assert await complete_stories(api_client, stream) == 0
        github.create_pull_request.assert_awaited_once()
        writes.assert_not_awaited()
        assert "PR_DETAIL_CANARY" not in json.dumps([row, logs])
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_stop_api_failure_is_visible_and_selected_again_next_cycle(
    api_client, completion_story
):
    story_id, _, _, _ = completion_story
    github = _github()
    stream = RedisStreamClient(os.environ["REDIS_URL"])
    await stream.connect()
    try:
        with (
            patch("src.tasks.story_completion.GitHubAppClient", return_value=github),
            patch.object(api_client, "stop_story", side_effect=ConnectionError("STOP_CANARY")),
            capture_logs() as logs,
        ):
            assert await complete_stories(api_client, stream) == 0
            assert await complete_stories(api_client, stream) == 0
        assert github.create_pull_request.await_count == 2
        row = await _read_row(story_id)
        assert row["status"] == "in_progress"
        assert row["quarantine_reason"] is None
        assert row["owner_notification"] is None
        assert story_id in {
            s.id for s in await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
        }
        assert any(log["event"] == "story_no_commits_stop_failed" for log in logs)
        assert "STOP_CANARY" not in json.dumps(logs)
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_new_commit_keeps_the_successful_pr_route(api_client, completion_story):
    story_id, _, _, _ = completion_story
    github = _github(empty=False)
    stream = RedisStreamClient(os.environ["REDIS_URL"])
    await stream.connect()
    try:
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=github):
            assert await complete_stories(api_client, stream) == 1
        story = await api_client.get_story(story_id)
        assert story.status is StoryStatus.PR_REVIEW
        assert story.pr_number == 7
        assert story.quarantine_reason is None
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_committed_empty_stop_survives_redis_interruption_and_is_delivered_later(
    api_client,
    completion_story,
):
    story_id, _, _, telegram_id = completion_story
    github = _github()
    first_process = RedisStreamClient(os.environ["REDIS_URL"])
    await first_process.connect()
    with patch("src.tasks.story_completion.GitHubAppClient", return_value=github):
        assert await complete_stories(api_client, first_process) == 0
    await first_process.close()
    assert (await _read_row(story_id))["owner_notification"]["state"] == "owed"
    # A new process reaches the same existing obligation. Its first Redis
    # publication fails; the row remains owed, with the ordinary delivery bound.
    with patch(
        "src.tasks.owner_notifications.deliver_to_admins",
        AsyncMock(side_effect=ConnectionError("admin unavailable")),
    ):
        await supervise_owed_owner_notifications(api_client, first_process)
    notice = await api_client.get_story_owner_notification(story_id)
    assert notice.state is OwnerNotificationState.OWED
    assert notice.admin_state is OwnerNotificationState.OWED
    await age_last_attempt_by_one_interval(story_id, notice.last_attempt_at, story_record=True)
    replacement = RedisStreamClient(os.environ["REDIS_URL"])
    await replacement.connect()
    try:
        newest = await replacement.redis.xrevrange(PO_INPUT_QUEUE, count=1)
        before = newest[0][0] if newest else "0-0"
        await supervise_owed_owner_notifications(api_client, replacement)
        unread = await replacement.redis.xread({PO_INPUT_QUEUE: before})
        events = [
            from_flat_fields(logical, POSystemEvent)
            for _, entries in unread
            for _, fields in entries
            if (logical := unprotect_po_payload(PO_INPUT_QUEUE, fields)).get("type")
            == "system_event"
        ]
        event = next(item for item in events if item.story_id == story_id)
        assert event.event == "story_blocked"
        assert event.telegram_chat_id == str(telegram_id)
        assert event.owner_notice.owed_at == notice.owed_at
        assert "nothing was produced" in event.text
        assert "a person" in event.text
        assert (
            await api_client.get_story_owner_notification(story_id)
        ).state is OwnerNotificationState.DELIVERED
    finally:
        await replacement.close()
