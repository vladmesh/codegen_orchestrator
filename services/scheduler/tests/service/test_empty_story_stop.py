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
