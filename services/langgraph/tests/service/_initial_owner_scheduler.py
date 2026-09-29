"""Real scheduler/API/Redis boundary for CI-only initial-owner recovery."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

from shared.notifications import AdminDeliveryResult
from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.pr_poller import poll_merged_prs
from src.tasks.supervisor.deploy import (
    supervise_deploying_stories,
    supervise_waiting_user_secret_stories,
)


async def run():
    api = SchedulerAPIClient()
    stream = RedisStreamClient()
    await stream.connect()
    story_id = os.environ["INITIAL_OWNER_STORY"]
    mode = os.environ["INITIAL_OWNER_MODE"]
    startup.init_config({"supervisor.resource_wait_timeout_minutes"})
    get_stories = api.get_stories_by_status
    list_notices = api.list_stories_owing_owner_notification

    async def selected(status):
        return [s for s in await get_stories(status) if s.id == story_id]

    async def selected_notices(**kwargs):
        return [s for s in await list_notices(**kwargs) if s.id == story_id]

    api.get_stories_by_status = selected
    api.list_stories_owing_owner_notification = selected_notices
    api.list_runs_owing_owner_notification = AsyncMock(return_value=[])
    try:
        if mode in {"retry", "infrastructure", "cancelled"}:
            # Fleet-readiness response is controlled, not a live host proof.
            with patch(
                "src.tasks.supervisor.deploy._admissible_target_exists",
                AsyncMock(return_value=True),
            ):
                counts = await supervise_deploying_stories(api, stream)
            assert counts["failed"] == 1
        elif mode == "owed":
            # The original API publish has returned its transport failure.
            # Advance only the handoff grace interval; retain native API/Redis.
            with patch("src.tasks.supervisor.deploy._qa_handoff_recovery_minutes", return_value=0):
                for _ in range(2):
                    counts = await supervise_deploying_stories(api, stream)
                    assert all(count == 0 for count in counts.values())
        elif mode == "secret":
            counts = await supervise_deploying_stories(api, stream)
            assert counts["waiting"] == 1
            story = await api.get_story(story_id)
            assert story.status.value == "waiting_user_secret"
            await api.request(
                "POST",
                f"projects/{story.project_id}/config/secrets",
                json={
                    "secrets": {"USER_SERVICE_KEY": "synthetic-user-service-value"},
                },
            )
            counts = await supervise_waiting_user_secret_stories(api, stream)
            assert counts["failed"] == 1 and counts["redispatched"] == 0
        elif mode == "poll":
            story = await api.get_story(story_id)
            pr = story.generated_product_timeline["pull_request"]
            github = AsyncMock()
            github.__aenter__.return_value = github
            github.get_pull_request.return_value = {
                **pr,
                "head": {"sha": pr["head_sha"]},
            }
            github.get_latest_workflow_run.return_value = {
                "id": 12345,
                "status": "completed",
                "conclusion": "success",
                "head_sha": pr["merge_commit_sha"],
                "created_at": pr["merged_at"],
                "html_url": "https://github.com/fixture/public/actions/runs/12345",
            }
            with (
                patch("src.tasks.pr_poller.GitHubAppClient", return_value=github),
                patch("src.tasks.pr_poller._ci_failure_log_excerpt_lines", return_value=10),
            ):
                assert await poll_merged_prs(api, stream) == 0
        elif mode in {"notice_interrupt", "notice"}:
            if mode == "notice_interrupt":
                stream.publish_flat = AsyncMock(
                    side_effect=ConnectionError("controlled Redis interruption")
                )
            # Native audience settlement uses explicit per-recipient delivery
            # truth. Telegram transport is synthetic; DB/Redis are real.
            with patch(
                "src.tasks.owner_notifications.deliver_to_admins",
                AsyncMock(
                    return_value=AdminDeliveryResult(configured=1, succeeded=1),
                ),
            ):
                await supervise_owed_owner_notifications(api, stream)
        else:
            raise AssertionError(mode)
    finally:
        await api.close()
        await stream.close()


asyncio.run(run())
