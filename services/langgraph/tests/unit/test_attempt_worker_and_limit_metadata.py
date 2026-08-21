"""An engineering attempt records the worker doing it, the limit, and why it stopped.

Without the first of those the supervisor cannot get from a stuck task to the
worker that is (or is not) working on it, and its only remaining evidence would
be the clock. The other two are what makes a failed run readable: the bound the
work was measured against, and which of the three stops it hit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.constants import Timeouts
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.queues.worker_result import WorkerStopReason

ATTEMPT_ID = "eng-abcdef123456"
OWNERSHIP = WorkerOwnership(
    project_id="00000000-0000-0000-0000-000000000001",
    run_id="live-run-1",
    attempt_id=ATTEMPT_ID,
)


class TestAttemptNamesItsWorker:
    @pytest.mark.asyncio
    async def test_recording_writes_worker_and_limit_onto_the_attempt(self):
        from src.clients.worker_spawner import record_worker_on_attempt

        with patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as patched:
            await record_worker_on_attempt(ATTEMPT_ID, "dev-story-1-abcd")

        path, kwargs = patched.await_args[0][0], patched.await_args[1]
        assert path == f"runs/{ATTEMPT_ID}"
        metadata = kwargs["json"]["run_metadata"]
        assert metadata["worker_id"] == "dev-story-1-abcd"
        assert metadata["agent_limit_seconds"] == Timeouts.AGENT_TURN

    @pytest.mark.asyncio
    async def test_the_limit_is_configurable_and_not_the_old_ceiling(self):
        """The recorded limit is the shared constant, and it is no longer 900s."""
        assert Timeouts.AGENT_TURN >= 45 * 60
        assert Timeouts.AGENT_TURN != 900

    @pytest.mark.asyncio
    async def test_a_reused_container_is_recorded_against_the_new_attempt(self):
        """A worker created for attempt A is executing attempt B when it is reused."""
        from src.clients import worker_spawner

        with (
            patch.object(
                worker_spawner, "record_worker_on_attempt", new_callable=AsyncMock
            ) as record,
            patch.object(
                worker_spawner, "record_turn_on_attempt", new_callable=AsyncMock
            ),
            patch.object(worker_spawner, "get_settings") as settings,
            patch.object(worker_spawner, "redis") as redis_mod,
        ):
            settings.return_value.redis_url = "redis://localhost:6379"
            client = AsyncMock()
            redis_mod.from_url.return_value = client
            client.xgroup_create = AsyncMock()
            client.xadd = AsyncMock()
            client.xreadgroup = AsyncMock(return_value=[])
            client.xgroup_destroy = AsyncMock()
            client.aclose = AsyncMock()

            await worker_spawner.send_task_to_worker(
                worker_id="dev-created-for-attempt-a",
                task_content="next task in the story",
                timeout_seconds=0,
                ownership=OWNERSHIP,
            )

        record.assert_awaited_once_with(ATTEMPT_ID, "dev-created-for-attempt-a")


class TestStopReasonReachesTheRun:
    @pytest.mark.asyncio
    async def test_a_turn_that_hit_its_limit_says_so_on_the_attempt(self):
        from src.consumers.engineering_result_handler import fail_job

        with patch("src.consumers.engineering_result_handler.api_client") as api:
            api.patch = AsyncMock()
            await fail_job(
                ATTEMPT_ID,
                "Agent process exceeded its 3600s turn limit",
                None,
                None,
                stop_reason=WorkerStopReason.AGENT_LIMIT_EXCEEDED,
                agent_limit_seconds=3600,
            )

        metadata = api.patch.await_args[1]["json"]["run_metadata"]
        assert metadata["stop_reason"] == WorkerStopReason.AGENT_LIMIT_EXCEEDED.value
        assert metadata["agent_limit_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_an_unexplained_failure_leaves_the_attempt_metadata_alone(self):
        """No stop reason means no metadata write — the merge must not blank the worker."""
        from src.consumers.engineering_result_handler import fail_job

        with patch("src.consumers.engineering_result_handler.api_client") as api:
            api.patch = AsyncMock()
            await fail_job(ATTEMPT_ID, "something else broke", None, None)

        assert "run_metadata" not in api.patch.await_args[1]["json"]

    @pytest.mark.asyncio
    async def test_a_refusal_is_the_third_stop_reason(self):
        from src.consumers.engineering_result_handler import handle_worker_gave_up

        redis = AsyncMock()
        with (
            patch("src.consumers.engineering_result_handler.api_client") as api,
            patch(
                "src.consumers.engineering_result_handler.publish_callback_event",
                new_callable=AsyncMock,
            ),
            patch(
                "src.consumers.engineering_result_handler.notify_admins_best_effort",
                new_callable=AsyncMock,
            ),
        ):
            api.patch = AsyncMock()
            api.post = AsyncMock()
            await handle_worker_gave_up(
                task_id=ATTEMPT_ID,
                project_id="00000000-0000-0000-0000-000000000001",
                planning_task_id=None,
                story_id=None,
                reason="the task asks for a credential I do not have",
                telegram_chat_id="",
                redis=redis,
            )

        metadata = api.patch.await_args_list[0][1]["json"]["run_metadata"]
        assert metadata["stop_reason"] == WorkerStopReason.AGENT_REFUSED.value
