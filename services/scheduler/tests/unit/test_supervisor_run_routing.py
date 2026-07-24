"""Tests for supervisor run-routing — DEPLOYING/TESTING stories routed by run outcome.

Split out of test_supervisor.py to keep each test module focused and small.
Shared DTO factories live in `_run_routing_factories`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Sibling test-helper module (not a test module); on sys.path via pytest prepend import mode.
from _run_routing_factories import (
    _invalid_result_error,
    _make_repo,
    _make_run,
    _make_story,
    _terminal_no_result_error,
)
import pytest

from shared.contracts.acceptance import BASELINE_ACCEPTANCE_CRITERIA
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.queues import DEPLOY_QUEUE, PO_INPUT_QUEUE

_WAITING_SECRET_RESULT = {
    "deploy_outcome": DeployOutcome.WAITING_FOR_USER_SECRET.value,
    "error_details": "Missing secrets: TELEGRAM_BOT_TOKEN",
    "missing_user_secrets": [
        {"key": "TELEGRAM_BOT_TOKEN", "description": "Telegram bot token from @BotFather"},
    ],
}


@pytest.fixture
def api_client():
    client = AsyncMock()
    # QA runs the repository's criteria, so the deploy→QA handoff resolves them.
    client.get_primary_repository.return_value = _make_repo()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    client.publish = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)
    client.redis.hdel = AsyncMock()
    client._redis = AsyncMock()
    client._redis.get = AsyncMock(return_value=None)
    client._redis.set = AsyncMock()
    client._redis.delete = AsyncMock()
    return client


class TestSuperviseDeployingStories:
    """Poll DEPLOYING stories and route based on deploy run outcome."""

    @pytest.mark.asyncio
    async def test_success_transitions_to_testing(self, api_client, redis_client):
        """SUCCESS outcome → story TESTING, QA message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
            },
        )
        api_client.transition_story.return_value = {}

        api_client.create_run.return_value = {"id": "qa-run-1"}

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "test")

        # QA run should be created
        api_client.create_run.assert_called_once()
        run_data = api_client.create_run.call_args[0][0]
        assert run_data["type"] == RunType.QA.value
        assert run_data["story_id"] == "story-1"

        # QA message should be published with run_id
        from shared.queues import QA_QUEUE

        qa_calls = [c for c in redis_client.publish_message.call_args_list if c[0][0] == QA_QUEUE]
        assert len(qa_calls) == 1
        qa_msg = qa_calls[0][0][1]
        assert qa_msg.deployed_url == "https://example.com"
        assert qa_msg.application_id == 42
        assert qa_msg.run_id  # run_id must be set
        # The criteria travel on the message — QA does not resolve them itself.
        assert qa_msg.acceptance_criteria == BASELINE_ACCEPTANCE_CRITERIA

    @pytest.mark.asyncio
    async def test_criteria_are_resolved_before_the_story_moves(self, api_client, redis_client):
        """The handoff carries the repository's criteria, whatever they say."""
        from src.tasks.supervisor import supervise_deploying_stories

        criteria = "- GET /health returns 200\n- Telegram: /start responds with welcome"
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
            },
        )
        api_client.get_primary_repository.return_value = _make_repo(acceptance_criteria=criteria)
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {"id": "qa-run-1"}

        await supervise_deploying_stories(api_client, redis_client)

        from shared.queues import QA_QUEUE

        qa_calls = [c for c in redis_client.publish_message.call_args_list if c[0][0] == QA_QUEUE]
        assert qa_calls[0][0][1].acceptance_criteria == criteria

    @pytest.mark.asyncio
    async def test_bot_username_comes_from_the_repository(self, api_client, redis_client):
        """QA gets the username stored at token validation, not the smoke result.

        The deploy smoke check often reports nothing, and QA errors out on a
        tg_bot project without a username — a false failure on a working bot.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
                "bot_username": None,
            },
        )
        api_client.get_primary_repository.return_value = _make_repo(bot_username="palindrome_bot")
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {"id": "qa-run-1"}

        await supervise_deploying_stories(api_client, redis_client)

        from shared.queues import QA_QUEUE

        qa_calls = [c for c in redis_client.publish_message.call_args_list if c[0][0] == QA_QUEUE]
        assert qa_calls[0][0][1].bot_username == "palindrome_bot"

    @pytest.mark.asyncio
    async def test_smoke_username_used_when_repository_has_none(self, api_client, redis_client):
        """Projects deployed before the username was persisted still reach QA."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
                "bot_username": "smoke_resolved_bot",
            },
        )
        api_client.get_primary_repository.return_value = _make_repo(bot_username=None)
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {"id": "qa-run-1"}

        await supervise_deploying_stories(api_client, redis_client)

        from shared.queues import QA_QUEUE

        qa_calls = [c for c in redis_client.publish_message.call_args_list if c[0][0] == QA_QUEUE]
        assert qa_calls[0][0][1].bot_username == "smoke_resolved_bot"

    @pytest.mark.parametrize(
        ("repo", "case"),
        [
            (None, "no primary repository"),
            (_make_repo(acceptance_criteria=None), "criteria never set"),
            (_make_repo(acceptance_criteria="   \n"), "criteria blank"),
        ],
    )
    @pytest.mark.asyncio
    async def test_success_without_criteria_fails_story(self, api_client, redis_client, repo, case):
        """No criteria → visible failure before TESTING, not a QA run that can only error.

        This is the `qa_no_acceptance_criteria` case: QA used to discover it after
        the story had already moved and a run had been created.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
            },
        )
        api_client.get_primary_repository.return_value = repo
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1, case
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()
        # No partial state: no story transition, no QA run created, no QA message.
        api_client.transition_story.assert_not_called()
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_without_application_id_fails_story(self, api_client, redis_client):
        """A success missing application_id can't reach QA → visible failure, no state change.

        `application_id` is optional on DeployRunResult, so the supervisor must guard the
        QA-handoff precondition before mutating the story or creating a QA run.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                # application_id intentionally absent
            },
        )
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()
        # No partial state: no story transition, no QA run created, no QA message.
        api_client.transition_story.assert_not_called()
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_give_up_fails_story(self, api_client, redis_client):
        """GIVE_UP outcome → story FAILED, admin notified."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.GIVE_UP.value,
                "error_details": "port already allocated",
            },
        )
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_code_fix_redispatches_to_engineering(self, api_client, redis_client):
        """CODE_FIX outcome → story IN_PROGRESS, engineering message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.CODE_FIX.value,
                "error_details": "ImportError: no module",
                "deploy_fix_attempt": 0,
            },
        )
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {}

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "start")

        from shared.queues import ENGINEERING_QUEUE

        eng_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1

    @pytest.mark.asyncio
    async def test_retry_republishes_deploy(self, api_client, redis_client):
        """RETRY outcome → new deploy run created, deploy message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={"triggered_by": "pr_poll", "head_sha": "a" * 40},
            result={"deploy_outcome": DeployOutcome.RETRY.value},
        )
        api_client.create_run.return_value = {}
        # First retry
        redis_client._redis.incr.return_value = 1

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["retried"] == 1
        from shared.queues import DEPLOY_QUEUE

        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert len(deploy_calls) == 1
        deploy_msg = deploy_calls[0][0][1]
        assert deploy_msg.head_sha == "a" * 40

        run_data = api_client.create_run.call_args[0][0]
        assert run_data["run_metadata"]["head_sha"] == "a" * 40

    @pytest.mark.asyncio
    async def test_retry_without_original_head_sha_fails_story(self, api_client, redis_client):
        """RETRY without source run head_sha fails instead of publishing a doomed deploy."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={"deploy_outcome": DeployOutcome.RETRY.value},
        )
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock):
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        api_client.create_run.assert_not_called()

        from shared.queues import DEPLOY_QUEUE

        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert deploy_calls == []

    @pytest.mark.asyncio
    async def test_head_sha_missing_fails_story_without_retry(self, api_client, redis_client):
        """HEAD_SHA_MISSING outcome → story failed, no generic exception path."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={"deploy_outcome": DeployOutcome.HEAD_SHA_MISSING.value},
        )
        api_client.fail_story.return_value = {}

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

        from shared.queues import DEPLOY_QUEUE

        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert deploy_calls == []

    @pytest.mark.asyncio
    async def test_retry_exhausted_fails_story(self, api_client, redis_client):
        """RETRY with max retries exceeded → story FAILED."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={"triggered_by": "pr_poll", "head_sha": "a" * 40},
            result={"deploy_outcome": DeployOutcome.RETRY.value},
        )
        api_client.fail_story.return_value = {}
        # Max retries hit
        redis_client._redis.incr.return_value = 3  # default max is 3

        with patch("src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock):
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_running_deploys(self, api_client, redis_client):
        """RUNNING deploy → skip (still in progress)."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.RUNNING, result=None
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {
            "tested": 0,
            "retried": 0,
            "redispatched": 0,
            "waiting": 0,
            "failed": 0,
        }
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_with_no_runs(self, api_client, redis_client):
        """DEPLOYING story with no runs → skip."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = None

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {
            "tested": 0,
            "retried": 0,
            "redispatched": 0,
            "waiting": 0,
            "failed": 0,
        }

    @pytest.mark.asyncio
    async def test_invalid_deploy_result_fails_story(self, api_client, redis_client):
        """Unparseable deploy result → story failed once, admin notified, no loop."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.side_effect = _invalid_result_error("deploy")
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_deploy_without_result_fails_story(self, api_client, redis_client):
        """A terminal deploy run that lost its result routes to a visible failure, not a skip."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.side_effect = _terminal_no_result_error("deploy")
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_deploy_is_skipped(self, api_client, redis_client):
        """A CANCELLED (superseded) deploy run has no result → skip, don't fail the story."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.CANCELLED, result=None
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {
            "tested": 0,
            "retried": 0,
            "redispatched": 0,
            "waiting": 0,
            "failed": 0,
        }
        api_client.fail_story.assert_not_called()
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_user_secret_parks_story_and_requests_once(
        self, api_client, redis_client
    ):
        """WAITING_FOR_USER_SECRET → story parked (not FAILED), one PO request emitted."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result=_WAITING_SECRET_RESULT,
        )
        api_client.get_project.return_value = SimpleNamespace(owner_id=555)

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["waiting"] == 1
        assert result["failed"] == 0
        # Parked, not failed.
        api_client.fail_story.assert_not_called()
        api_client.wait_user_secret_story.assert_called_once_with("story-1")

        # Exactly one PO request on po:input, carrying the key + description, not consumers.
        po_calls = [
            c for c in redis_client.publish_flat.call_args_list if c[0][0] == PO_INPUT_QUEUE
        ]
        assert len(po_calls) == 1
        fields = po_calls[0][0][1]
        assert fields["event"] == "story_waiting_user_secret"
        assert fields["user_id"] == "555"
        assert "TELEGRAM_BOT_TOKEN" in fields["text"]
        assert "Telegram bot token" in fields["text"]


class TestSuperviseWaitingUserSecretStories:
    """Poll WAITING_USER_SECRET stories; re-deploy once the secret is saved."""

    @pytest.mark.asyncio
    async def test_redispatch_when_all_secrets_present(self, api_client, redis_client):
        """All missing keys saved → new deploy run + DEPLOYING, no repeated user message."""
        from src.tasks.supervisor import supervise_waiting_user_secret_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="waiting_user_secret")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={"triggered_by": "pr_poll", "head_sha": "a" * 40},
            result=_WAITING_SECRET_RESULT,
        )
        api_client.list_project_secret_keys.return_value = ["TELEGRAM_BOT_TOKEN", "OTHER"]
        api_client.create_run.return_value = {}

        result = await supervise_waiting_user_secret_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "deploy")

        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert len(deploy_calls) == 1
        assert deploy_calls[0][0][1].head_sha == "a" * 40
        run_data = api_client.create_run.call_args[0][0]
        assert run_data["run_metadata"]["head_sha"] == "a" * 40

        # No repeated request to the user — the request is one-shot on entry to the wait.
        po_calls = [
            c for c in redis_client.publish_flat.call_args_list if c[0][0] == PO_INPUT_QUEUE
        ]
        assert po_calls == []

    @pytest.mark.asyncio
    async def test_no_redispatch_when_secret_still_missing(self, api_client, redis_client):
        """Incomplete secret set → story stays waiting, nothing published, no message."""
        from src.tasks.supervisor import supervise_waiting_user_secret_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="waiting_user_secret")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={"triggered_by": "pr_poll", "head_sha": "a" * 40},
            result=_WAITING_SECRET_RESULT,
        )
        api_client.list_project_secret_keys.return_value = ["OTHER"]

        result = await supervise_waiting_user_secret_stories(api_client, redis_client)

        assert result == {"redispatched": 0, "failed": 0}
        api_client.transition_story.assert_not_called()
        api_client.fail_story.assert_not_called()
        redis_client.publish_message.assert_not_called()
        po_calls = [
            c for c in redis_client.publish_flat.call_args_list if c[0][0] == PO_INPUT_QUEUE
        ]
        assert po_calls == []

    @pytest.mark.asyncio
    async def test_redispatch_without_head_sha_fails_story(self, api_client, redis_client):
        """Secrets present but no source head_sha → typed failure, no doomed deploy."""
        from src.tasks.supervisor import supervise_waiting_user_secret_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="waiting_user_secret")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result=_WAITING_SECRET_RESULT,
        )
        api_client.list_project_secret_keys.return_value = ["TELEGRAM_BOT_TOKEN"]

        with patch("src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock):
            result = await supervise_waiting_user_secret_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_story_with_failed_run_is_not_swept_to_failed(
        self, api_client, redis_client
    ):
        """No supervisor fails a WAITING_USER_SECRET story just because its run is FAILED."""
        from src.tasks.supervisor import (
            supervise_deploying_stories,
            supervise_waiting_user_secret_stories,
        )

        def _by_status(status):
            if status == StoryStatus.WAITING_USER_SECRET:
                return [_make_story(id="story-1", status="waiting_user_secret")]
            return []

        api_client.get_stories_by_status.side_effect = _by_status
        # Latest deploy run is terminal FAILED (the run that hit the missing secret).
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={"head_sha": "a" * 40},
            result=_WAITING_SECRET_RESULT,
        )
        # Secret still not saved, so the story must simply keep waiting.
        api_client.list_project_secret_keys.return_value = []

        deploying = await supervise_deploying_stories(api_client, redis_client)
        waiting = await supervise_waiting_user_secret_stories(api_client, redis_client)

        assert deploying["failed"] == 0
        assert waiting == {"redispatched": 0, "failed": 0}
        api_client.fail_story.assert_not_called()
        api_client.transition_story.assert_not_called()


class TestSuperviseTestingStories:
    """Poll TESTING stories and route based on QA run outcome."""

    @pytest.mark.asyncio
    async def test_passed_completes_story(self, api_client, redis_client):
        """PASSED outcome → story COMPLETED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.PASSED.value,
                "deployed_url": "https://example.com",
            },
        )
        api_client.transition_story.return_value = {}

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["completed"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "complete")

    @pytest.mark.asyncio
    async def test_failed_creates_fix_task_and_redispatches(self, api_client, redis_client):
        """FAILED outcome → fix task created, story back to IN_PROGRESS, engineering redispatch."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "Weather endpoint broken",
                "failed_checks": [{"name": "weather", "detail": "404"}],
                "qa_attempt": 0,
            },
        )
        api_client.transition_story.return_value = {}
        api_client.create_task.return_value = {"id": "task-fix-1"}

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "start")
        api_client.create_task.assert_called_once()
        task_data = api_client.create_task.call_args[0][0]
        assert task_data["story_id"] == "story-1"
        assert task_data["status"] == "todo"
        assert "weather" in task_data["description"].lower()

    @pytest.mark.asyncio
    async def test_exhausted_fails_story(self, api_client, redis_client):
        """EXHAUSTED outcome → story FAILED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.EXHAUSTED.value,
                "summary": "Still broken after 2 attempts",
                "qa_attempt": 2,
            },
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_error_fails_story(self, api_client, redis_client):
        """ERROR outcome → story FAILED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.ERROR.value,
                "error": "bot_username missing",
            },
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_running_qa(self, api_client, redis_client):
        """QA run still RUNNING → skip, no action."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.RUNNING, result=None
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_no_testing_stories(self, api_client, redis_client):
        """No TESTING stories → zero counts."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = []

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_no_qa_runs_skips(self, api_client, redis_client):
        """TESTING story with no QA runs → skip."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = None

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_invalid_qa_result_fails_story(self, api_client, redis_client):
        """Unparseable QA result → story failed once, admin notified, no loop."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.side_effect = _invalid_result_error("qa")
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_qa_without_result_fails_story(self, api_client, redis_client):
        """A terminal QA run that lost its result routes to a visible failure, not a skip."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.side_effect = _terminal_no_result_error("qa")
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()
