"""Exercise poller, normal dispatch and both notice audiences in a separate service."""

import asyncio
from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock, patch

import structlog

from shared.notifications import AdminDeliveryResult
from shared.redis import RedisStreamClient
from src import startup
from src.clients.api import SchedulerAPIClient
from src.tasks.owner_notifications import deliver_owed_notification
from src.tasks.pr_poller import poll_merged_prs
from src.tasks.supervisor.liveness import supervise_failed_tasks
from src.tasks.task_dispatcher import dispatch_todo_tasks

STORY = os.environ["CONFLICT_STORY"]


class ScopedAPI(SchedulerAPIClient):
    # Other tests share this database; candidate reads stay at the real API.
    async def get_stories_by_status(self, status):
        return [row for row in await super().get_stories_by_status(status) if row.id == STORY]

    async def get_tasks_by_status(self, status):
        return [row for row in await super().get_tasks_by_status(status) if row.story_id == STORY]


class SyntheticGitHub:
    merged = False
    refreshed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner, repo, number):
        return {
            "number": number,
            "state": "closed" if self.merged else "open",
            "merged_at": datetime.now(UTC).isoformat() if self.merged else None,
            "merge_commit_sha": "c" * 40 if self.merged else None,
            "mergeable_state": "clean" if os.environ["CONFLICT_MODE"] == "clean" else "dirty",
            "head": {"sha": "a" * 40},
        }

    async def refresh_registry_secrets(self, *args):
        self.refreshed = True

    async def merge_pull_request(self, *args):
        assert self.refreshed
        self.merged = True
        return {"merged": True}

    async def get_latest_workflow_run(self, *args, **kwargs):
        return None


async def exercise():
    startup.init_config(["scheduler.ci_failure_log_excerpt_lines"])
    api = ScopedAPI()
    stream = RedisStreamClient()
    await stream.connect()
    try:
        mode = os.environ["CONFLICT_MODE"]
        if mode in {"poll", "clean"}:
            github = SyntheticGitHub()
            with patch("src.tasks.pr_poller.GitHubAppClient", return_value=github):
                assert await poll_merged_prs(api, stream) == 0
            if mode == "clean":
                assert github.merged and github.refreshed
        elif mode == "dispatch":
            assert await dispatch_todo_tasks(api, stream) == 1
            assert await dispatch_todo_tasks(api, stream) == 0
        elif mode == "fail-lost":
            request = api.request
            lost = False

            async def lose_committed_response(method, path, **kwargs):
                nonlocal lost
                response = await request(method, path, **kwargs)
                if method == "POST" and path.endswith("/attempt-outcome") and not lost:
                    lost = True
                    raise OSError("synthetic committed retry response loss")
                return response

            with patch.object(api, "request", lose_committed_response):
                assert await supervise_failed_tasks(api, stream) == {"retried": 0, "escalated": 0}
            assert lost
            assert await supervise_failed_tasks(api, stream) == {"retried": 0, "escalated": 0}
        elif mode == "fail":
            outcome = await supervise_failed_tasks(api, stream)
            assert outcome["retried"] + outcome["escalated"] == 1, outcome
        else:
            record = await api.get_story_owner_notification(STORY)
            with patch(
                "src.tasks.owner_notifications.deliver_to_admins",
                AsyncMock(return_value=AdminDeliveryResult(configured=1, succeeded=1)),
            ) as admins:
                await deliver_owed_notification(
                    api, stream, STORY, record, structlog.get_logger(), story_record=True
                )
                admins.assert_awaited_once()
    finally:
        await stream.close()
        await api.close()


asyncio.run(exercise())
