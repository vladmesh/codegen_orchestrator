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
from src.tasks.supervisor.liveness import supervise_failed_tasks, supervise_stuck_tasks
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
        elif mode == "dispatch-interrupt":
            with patch.object(
                api,
                "start_engineering_attempt",
                AsyncMock(side_effect=OSError("status write unavailable")),
            ):
                assert await dispatch_todo_tasks(api, stream) == 0
        elif mode == "dispatch-pause-start":
            await delayed_dispatch_start(api, stream)
        elif mode == "dispatch-start-lost":
            await dispatch_with_lost_start(api, stream)
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
        elif mode in {"stuck", "stuck-concurrent-lost"}:
            if mode == "stuck-concurrent-lost":
                await concurrent_terminal_recovery(api, stream)
            # Later native ticks must not create another settlement episode.
            for _ in range(2):
                await supervise_stuck_tasks(api, stream)
                assert await supervise_failed_tasks(api, stream) == {"retried": 0, "escalated": 0}
        else:
            record = await api.get_story_owner_notification(STORY)
            with patch(
                "src.tasks.owner_notifications.deliver_to_admins",
                AsyncMock(return_value=AdminDeliveryResult(configured=1, succeeded=1)),
            ) as admins:
                await deliver_owed_notification(
                    api, stream, STORY, record, structlog.get_logger(), story_record=True
                )
                if mode == "notice-repeat":
                    admins.assert_not_awaited()
                else:
                    admins.assert_awaited_once()
    finally:
        await stream.close()
        await api.close()


async def delayed_dispatch_start(api, stream):
    start = api.start_engineering_attempt

    async def delayed_start(command):
        # XADD has completed; release this real start only after the
        # actual consumer's failed Run and retry transaction commit.
        await stream.redis.rpush(f"conflict-start-ready:{STORY}", command.run_id)
        assert await stream.redis.blpop(f"conflict-start-release:{STORY}", timeout=10)
        result = await start(command)
        assert result.outcome.value == "settled"
        return result

    with patch.object(api, "start_engineering_attempt", delayed_start):
        assert await dispatch_todo_tasks(api, stream) == 0


async def dispatch_with_lost_start(api, stream):
    start = api.start_engineering_attempt
    lost = False

    async def lost_start(command):
        nonlocal lost
        result = await start(command)
        if not lost:
            lost = True
            raise OSError("committed start response lost")
        assert result.outcome.value == "reused"
        return result

    with patch.object(api, "start_engineering_attempt", lost_start):
        assert await dispatch_todo_tasks(api, stream) == 1
    assert lost


async def concurrent_terminal_recovery(api, stream):
    request = api.request
    lost = False
    discovered = 0
    ready = asyncio.Event()

    async def rendezvous(method, path):
        nonlocal discovered
        if method == "POST" and path.endswith("/attempt-outcome"):
            discovered += 1
            if discovered == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)

    async def lose_settlement_response(method, path, **kwargs):
        nonlocal lost
        await rendezvous(method, path)
        response = await request(method, path, **kwargs)
        if method == "POST" and path.endswith("/attempt-outcome") and not lost:
            lost = True
            raise OSError("synthetic terminal settlement response loss")
        return response

    other = ScopedAPI()
    other_request = other.request

    async def concurrent_request(method, path, **kwargs):
        await rendezvous(method, path)
        return await other_request(method, path, **kwargs)

    try:
        with (
            patch.object(api, "request", lose_settlement_response),
            patch.object(other, "request", concurrent_request),
        ):
            await asyncio.gather(
                supervise_stuck_tasks(api, stream),
                supervise_stuck_tasks(other, stream),
            )
        assert lost
    finally:
        await other.close()


asyncio.run(exercise())
