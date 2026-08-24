"""Tests for supervisor run-routing — DEPLOYING/TESTING stories routed by run outcome.

Split out of test_supervisor.py to keep each test module focused and small.
Shared DTO factories live in `_run_routing_factories`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

# Sibling test-helper module (not a test module); on sys.path via pytest prepend import mode.
from _run_routing_factories import (
    _invalid_result_error,
    _make_project,
    _make_repo,
    _make_run,
    _make_story,
    _terminal_no_result_error,
)
import pytest

from shared.contracts.acceptance import BASELINE_ACCEPTANCE_CRITERIA
from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetAdmissionRead,
    EngineeringBudgetReservationState,
)
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.user import UserDTO
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, PO_INPUT_QUEUE
from shared.tests.allocation_routing_cases import (
    REFUSAL_ROUTING_CASES,
    refused_deploy_result,
)
from shared.tests.server_admission_cases import (
    ADMISSION_CASES,
    admission_case_incidents,
    admission_case_server,
)

_WAITING_SECRET_RESULT = {
    "deploy_outcome": DeployOutcome.WAITING_FOR_USER_SECRET.value,
    "error_details": "Missing secrets: TELEGRAM_BOT_TOKEN",
    "missing_user_secrets": [
        {"key": "TELEGRAM_BOT_TOKEN", "description": "Telegram bot token from @BotFather"},
    ],
}


def _engineering_admission(
    outcome: EngineeringBudgetAdmissionOutcome = EngineeringBudgetAdmissionOutcome.ADMITTED,
) -> EngineeringBudgetAdmissionRead:
    """A typed admission result, so a deleted dispatch guard cannot pass silently."""
    return EngineeringBudgetAdmissionRead(
        attempt_id="eng-deploy-fix-deploy-1-1",
        user_id=1,
        outcome=outcome,
        reservation_microusd=60 if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else 0,
        known_spend_microusd=40,
        active_held_microusd=60 if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else 0,
        available_microusd=60,
        reservation_state=(
            EngineeringBudgetReservationState.ACTIVE
            if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED
            else None
        ),
    )


def _resolved_user(user_id: int) -> UserDTO:
    """A user whose Telegram chat id is deliberately nothing like their User.id."""
    return UserDTO(
        id=user_id,
        telegram_id=900000000 + user_id,
        is_admin=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _qa_handoff_plan(**overrides) -> dict:
    """The handoff a QA run carries, as it is stored in `run_metadata`.

    The supervisor reads the deployment's address back off it when the story is
    completed, so the user is given the deployment QA actually tested.
    """
    message = {
        "story_id": "story-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "initiating_run_id": "live-1",
        "telegram_chat_id": "",
        "deployed_url": "https://example.com",
        "application_id": 42,
        "acceptance_criteria": BASELINE_ACCEPTANCE_CRITERIA,
        "bot_username": "palindrome_bot",
        "run_id": "qa-1",
    }
    message.update(overrides)
    return QAHandoffPlan(qa_message=message).model_dump(mode="json")


@pytest.fixture
def api_client():
    client = AsyncMock()
    # QA runs the repository's criteria, so the deploy→QA handoff resolves them.
    client.get_primary_repository.return_value = _make_repo()
    # The project the story belongs to: the deploy→QA and deploy→fix handoffs
    # read the run that initiated the work off it.
    client.get_project.return_value = _make_project()
    # Most stories borrow no temporary access; the ones that do say so.
    client.get_live_temporary_access_grant_for_run.return_value = None
    # The owner's internal id is not their Telegram chat: resolution goes
    # through the users API, and the two numbers must never be confused.
    client.get_user.side_effect = _resolved_user
    # A terminal owner notice is published only once the story has been read
    # back and found in the status the transition put it in, so the double
    # answers that read the way the API would after the escalation committed.
    client.get_story.return_value = _make_story(id="story-1", status="waiting_human_review")
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
        api_client.create_run_if_absent.assert_called_once()
        run_data = api_client.create_run_if_absent.call_args[0][0]
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
        # The QA executor this message leads to belongs to the run that asked
        # for the work — the project's, the same one the developer workers
        # carried — and not to the QA run row, which is only this attempt.
        assert qa_msg.initiating_run_id == "live-run-1"
        assert qa_msg.initiating_run_id != qa_msg.run_id
        # The criteria travel on the message — QA does not resolve them itself.
        assert qa_msg.acceptance_criteria == BASELINE_ACCEPTANCE_CRITERIA
        metadata = api_client.create_run_if_absent.call_args.args[0]["run_metadata"]
        assert metadata["application_id"] == 42
        # The plan is stored with the run, so a restart can finish this handoff.
        assert QAHandoffPlan.model_validate(metadata[QA_HANDOFF_KEY]).access is None

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
    async def test_success_on_a_project_without_an_initiating_run_fails_story(
        self, api_client, redis_client
    ):
        """A project that predates run ownership gets no QA executor at all.

        Its initiating run was never recorded, and nothing may be substituted for
        it: a project id or a minted id would reach the executor's
        `com.codegen.run.id` label as if it were the run that asked for the work.
        So the handoff fails visibly instead, leaving no QA run and no message.
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
        api_client.get_project.return_value = _make_project(initiating_run_id=None)
        api_client.fail_story.return_value = {}

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()
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
        """CODE_FIX admission precedes its exact-id run and engineering handoff."""
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
        api_client.admit_engineering_budget.return_value = _engineering_admission()

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        admission = api_client.admit_engineering_budget.await_args.args[0]
        assert admission.attempt_id == "eng-deploy-fix-deploy-1-1"
        assert admission.task_id == admission.attempt_id
        assert admission.story_id == "story-1"
        assert admission.project_id == _make_project().id
        admission_call = next(
            index
            for index, call in enumerate(api_client.mock_calls)
            if call[0] == "admit_engineering_budget"
        )
        create_run_call = next(
            index for index, call in enumerate(api_client.mock_calls) if call[0] == "create_run"
        )
        assert admission_call < create_run_call
        api_client.transition_story.assert_called_once_with("story-1", "start")

        eng_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1

    @pytest.mark.asyncio
    async def test_code_fix_budget_denial_parks_story_without_engineering_handoff(
        self, api_client, redis_client
    ):
        """A denied deploy fix leaves no Run, queue work, or retryable deploy state."""
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
        api_client.admit_engineering_budget.return_value = _engineering_admission(
            EngineeringBudgetAdmissionOutcome.DENIED
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 0
        assert result["failed"] == 1
        api_client.create_run.assert_not_awaited()
        api_client.release_engineering_budget_admission.assert_not_awaited()
        assert not [
            call
            for call in redis_client.publish_message.call_args_list
            if call.args[0] == ENGINEERING_QUEUE
        ]
        api_client.update_story.assert_awaited_once_with(
            "story-1",
            {
                "quarantine_reason": {
                    "reason": "engineering_budget_denied",
                    "attempt_id": "eng-deploy-fix-deploy-1-1",
                    "known_spend_microusd": 40,
                    "active_held_microusd": 0,
                    "available_microusd": 60,
                }
            },
        )
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        assert any(
            call.args[0] == PO_INPUT_QUEUE for call in redis_client.publish_flat.call_args_list
        )

    @pytest.mark.asyncio
    async def test_code_fix_publish_failure_releases_exact_admission_before_handoff(
        self, api_client, redis_client
    ):
        """A failed deploy-fix publish proves no provider work started, so release its hold."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.CODE_FIX.value,
                "deploy_fix_attempt": 0,
            },
        )
        api_client.admit_engineering_budget.return_value = _engineering_admission()
        redis_client.publish_message.side_effect = RuntimeError("redis unavailable")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 0
        api_client.release_engineering_budget_admission.assert_awaited_once_with(
            "eng-deploy-fix-deploy-1-1"
        )
        api_client.update_run.assert_awaited_once()
        assert api_client.update_run.await_args.args[0] == "eng-deploy-fix-deploy-1-1"

    @pytest.mark.asyncio
    async def test_code_fix_run_creation_failure_releases_exact_admission_before_handoff(
        self, api_client, redis_client
    ):
        """A deploy-fix Run failure is pre-handoff and cannot strand an active hold."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.CODE_FIX.value,
                "deploy_fix_attempt": 0,
            },
        )
        api_client.admit_engineering_budget.return_value = _engineering_admission()
        api_client.create_run.side_effect = RuntimeError("api unavailable")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 0
        api_client.release_engineering_budget_admission.assert_awaited_once_with(
            "eng-deploy-fix-deploy-1-1"
        )
        redis_client.publish_message.assert_not_awaited()
        api_client.update_run.assert_not_awaited()

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
    async def test_cancelled_deploy_is_redeployed_not_left_waiting(self, api_client, redis_client):
        """A cancelled deploy neither failed nor deployed, so the story is owed one.

        A temporary-access revoke fences every unfinished deploy.yml run of the
        repository, which cancels ordinary story deploys as a matter of course.
        Without a route the story would sit in DEPLOYING with a run every
        supervisor skips.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.CANCELLED,
            run_metadata={"head_sha": "a" * 40},
            result={"deploy_outcome": DeployOutcome.CANCELLED.value},
        )
        api_client.create_run.return_value = {}
        redis_client._redis.incr.return_value = 1

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["retried"] == 1
        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert len(deploy_calls) == 1
        assert deploy_calls[0][0][1].head_sha == "a" * 40

    @pytest.mark.asyncio
    async def test_a_cancelled_deploy_stops_looping_at_the_retry_bound(
        self, api_client, redis_client
    ):
        """The redeploy is bounded by the same counter that stops a failing deploy."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.CANCELLED,
            run_metadata={"head_sha": "a" * 40},
            result={"deploy_outcome": DeployOutcome.CANCELLED.value},
        )
        redis_client._redis.incr.return_value = 3

        with patch("src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock):
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

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
            "escalated": 0,
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
            "escalated": 0,
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
            "escalated": 0,
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
        # owner_id 555 is a User.id; the event carries the chat it resolves to.
        assert fields["telegram_chat_id"] == "900000555"
        assert fields["owner_user_id"] == "555"
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
            # Every QA run carries the handoff it was dispatched with; it is
            # written with the run, before the story leaves DEPLOYING.
            run_metadata={QA_HANDOFF_KEY: _qa_handoff_plan()},
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
    async def test_existing_fix_task_recovers_story_transition_after_partial_failure(
        self, api_client, redis_client
    ):
        """A saved fix task for this QA run still returns the story to engineering."""
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
            },
        )
        existing_task = AsyncMock()
        existing_task.failure_metadata = {"qa_failure": {"qa_run_id": "qa-1"}}
        api_client.get_tasks_by_story.return_value = [existing_task]

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 0,
            "recovered": 0,
        }
        api_client.create_task.assert_not_awaited()
        api_client.transition_story.assert_awaited_once_with("story-1", "start")

    @pytest.mark.asyncio
    async def test_three_identical_failures_create_two_fixes_then_wait_for_human(
        self, api_client, redis_client
    ):
        """The third matching QA failure is retained for a human, not reworked again."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        prior = []
        for run_id in ("qa-1", "qa-2"):
            api_client.get_tasks_by_story.return_value = prior
            api_client.get_latest_run_by_story.return_value = _make_run(
                id=run_id,
                type=RunType.QA,
                result={
                    "qa_outcome": QAOutcome.FAILED.value,
                    "summary": "Weather endpoint broken",
                    "failed_checks": [{"name": "weather", "detail": "404"}],
                },
            )

            result = await supervise_testing_stories(api_client, redis_client)

            assert result["redispatched"] == 1
            created = api_client.create_task.call_args.args[0]
            task = AsyncMock()
            task.failure_metadata = created["failure_metadata"]
            prior.append(task)

        api_client.get_tasks_by_story.return_value = prior
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-3",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "Weather endpoint broken",
                "failed_checks": [{"name": "weather", "detail": "404"}],
            },
        )
        api_client.create_task.reset_mock()

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 1,
            "recovered": 0,
        }
        api_client.create_task.assert_not_awaited()
        api_client.update_story.assert_awaited_with(
            "story-1",
            {
                "quarantine_reason": {
                    "qa_outcome": "failed",
                    "qa_failure": {
                        "qa_run_id": "qa-3",
                        "fingerprint": ANY,
                        "fingerprint_attempt": 3,
                        "fix_attempt": 3,
                        "summary": "Weather endpoint broken",
                        "failed_checks": [{"name": "weather", "detail": "404"}],
                    },
                }
            },
        )
        api_client.transition_story.assert_awaited_with("story-1", "human-review")

    @pytest.mark.asyncio
    async def test_qa_fix_ceiling_escalates_even_for_a_new_failure_signature(
        self, api_client, redis_client
    ):
        """A changing symptom cannot keep the QA to fix loop running forever."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        prior = []
        for run_id, detail in (("qa-1", "404"), ("qa-2", "500")):
            api_client.get_tasks_by_story.return_value = prior
            api_client.get_latest_run_by_story.return_value = _make_run(
                id=run_id,
                type=RunType.QA,
                result={
                    "qa_outcome": QAOutcome.FAILED.value,
                    "summary": "Weather endpoint broken",
                    "failed_checks": [{"name": "weather", "detail": detail}],
                },
            )
            await supervise_testing_stories(api_client, redis_client)
            task = AsyncMock()
            task.failure_metadata = api_client.create_task.call_args.args[0]["failure_metadata"]
            prior.append(task)

        api_client.get_tasks_by_story.return_value = prior
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-3",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "New weather endpoint failure",
                "failed_checks": [{"name": "weather", "detail": "403"}],
            },
        )
        api_client.create_task.reset_mock()

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.create_task.assert_not_awaited()
        api_client.transition_story.assert_awaited_with("story-1", "human-review")

    @pytest.mark.asyncio
    async def test_exhausted_quarantines_application_and_notifies_owner(
        self, api_client, redis_client
    ):
        """EXHAUSTED outcome stops the app without releasing its token."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.EXHAUSTED.value,
                "summary": "Still broken after 2 attempts",
                "qa_attempt": 2,
            },
        )

        api_client.get_project.return_value = SimpleNamespace(owner_id=100713)

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.stop_application.assert_awaited_once_with(42)
        api_client.update_story.assert_awaited_once_with(
            "story-1",
            {
                "quarantine_reason": {
                    "qa_outcome": QAOutcome.EXHAUSTED.value,
                    "summary": "Still broken after 2 attempts",
                }
            },
        )
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        api_client.fail_story.assert_not_called()
        event = redis_client.publish_flat.await_args.args[1]
        assert event["event"] == "story_quarantined"
        assert event["telegram_chat_id"] == "900100713"
        assert event["owner_user_id"] == "100713"

    @pytest.mark.asyncio
    async def test_error_quarantines_application(self, api_client, redis_client):
        """ERROR outcome stops the app and preserves the QA error on the story."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.ERROR.value,
                "error": "bot_username missing",
            },
        )

        api_client.get_project.return_value = SimpleNamespace(owner_id=100713)

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.stop_application.assert_awaited_once_with(42)
        api_client.update_story.assert_awaited_once_with(
            "story-1",
            {
                "quarantine_reason": {
                    "qa_outcome": QAOutcome.ERROR.value,
                    "error": "bot_username missing",
                }
            },
        )
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        api_client.fail_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_qa_blocker_waits_for_human_without_creating_fix_task(
        self, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.BLOCKED.value,
                "blocker": {
                    "category": "unknown",
                    "attempted": "run Claude Code QA command",
                    "sent": "timeout 1200 claude -p ...",
                    "received": "exit_status=1; stdout=; stderr=timeout",
                },
                "state_changes": [
                    {
                        "resource": "user telegram_id=8202532144",
                        "operation": "created",
                        "cleanup": {
                            "attempted": True,
                            "succeeded": True,
                            "detail": "DELETE /users/8202532144 returned 204",
                        },
                    }
                ],
            },
        )

        api_client.get_project.return_value = SimpleNamespace(owner_id=100713)

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.stop_application.assert_awaited_once_with(42)
        reason = api_client.update_story.await_args.args[1]["quarantine_reason"]
        assert reason["blocker"] == {
            "category": "unknown",
            "attempted": "run Claude Code QA command",
            "sent": "timeout 1200 claude -p ...",
            "received": "exit_status=1; stdout=; stderr=timeout",
        }
        assert reason["state_changes"] == [
            {
                "resource": "user telegram_id=8202532144",
                "operation": "created",
                "cleanup": {
                    "attempted": True,
                    "succeeded": True,
                    "detail": "DELETE /users/8202532144 returned 204",
                },
            }
        ]
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        api_client.create_task.assert_not_called()
        api_client.fail_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_undelivered_telegram_probe_never_creates_an_engineering_fix(
        self, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-telegram-1",
            type=RunType.QA,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.BLOCKED.value,
                "blocker": {
                    "category": "telegram_probe_undelivered",
                    "attempted": "send an empty message to @weather_bot",
                    "sent": "",
                    "received": "ValueError: The message cannot be empty",
                },
                "telegram_probe_evidence": [
                    {
                        "action": "message",
                        "attempted": "send an empty message to @weather_bot",
                        "sent": "",
                        "delivered": False,
                        "replies": [],
                        "callback": None,
                        "error": "ValueError: The message cannot be empty",
                    }
                ],
            },
        )
        api_client.get_project.return_value = SimpleNamespace(owner_id=100713)

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.create_task.assert_not_awaited()
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")

    @pytest.mark.asyncio
    async def test_unreachable_health_only_qa_waits_for_human_without_fix_task(
        self, api_client, redis_client
    ):
        """A health-check transport failure is a review blocker, not a product defect."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.BLOCKED.value,
                "blocker": {
                    "category": "deployed_url_unreachable",
                    "attempted": "GET deployed URL before starting QA agent",
                    "sent": "GET https://example.com",
                    "received": "transport error: connection refused",
                },
            },
        )

        api_client.get_project.return_value = SimpleNamespace(owner_id=100713)

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.stop_application.assert_awaited_once_with(42)
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        api_client.create_task.assert_not_called()
        api_client.fail_story.assert_not_called()

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

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 0,
            "recovered": 0,
        }

    @pytest.mark.asyncio
    async def test_no_testing_stories(self, api_client, redis_client):
        """No TESTING stories → zero counts."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = []

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 0,
            "recovered": 0,
        }

    @pytest.mark.asyncio
    async def test_no_qa_runs_skips(self, api_client, redis_client):
        """TESTING story with no QA runs → skip."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = None

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 0,
            "recovered": 0,
        }

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


class TestDeployRefusedByAdmission:
    """A deploy the platform could not place must never terminate the story, and
    each refusal disposition must get its own behaviour.

    The run result these feed to the real routing is the one
    `shared.tests.allocation_routing_cases` describes, and the langgraph suite
    asserts the deploy consumer writes exactly that — so this is the receiving
    end of the same boundary, not a restatement of it. The expected behaviour
    travels with each case, so a path that answers two dispositions the same way
    fails here. The previous version of this class expected one identical wait
    for every reason, which is why it could not see the deploy path collapsing
    `IMPOSSIBLE_CAPACITY` into an endless poll.
    """

    @staticmethod
    def _refused_run(reason=AllocationFailureReason.SERVER_NOT_PROVISIONED, **overrides):
        run_metadata = {"head_sha": "b" * 40}
        run_metadata.update(overrides.pop("run_metadata", {}))
        return _make_run(
            status=RunStatus.FAILED,
            run_metadata=run_metadata,
            result=refused_deploy_result(reason).model_dump(mode="json"),
            **overrides,
        )

    @staticmethod
    def _fleet(api_client, *, admissible: bool):
        """The fleet the wait re-checks: one host, admissible or still installing."""
        case = next(
            candidate
            for candidate in ADMISSION_CASES
            if candidate.admitted is admissible
            and (admissible or candidate.name == "active_while_installing_software")
        )
        now = datetime.now(UTC)
        api_client.get_servers.return_value = [admission_case_server(case, last_health_check=now)]
        api_client.list_active_incidents.return_value = admission_case_incidents(
            case, detected_at=now
        )
        api_client.get_applications.return_value = []

    @staticmethod
    def _published_events(redis_client) -> list[str]:
        return [call.args[1]["event"] for call in redis_client.publish_flat.call_args_list]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", REFUSAL_ROUTING_CASES, ids=lambda case: case.reason.value)
    async def test_each_disposition_routes_the_way_the_matrix_says(
        self, api_client, redis_client, case
    ):
        """With no admissible target: one behaviour per disposition, all distinct."""
        from src.tasks.supervisor import supervise_deploying_stories

        expected = case.deploy
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run(case.reason)
        api_client.get_project.return_value = SimpleNamespace(owner_id=555)
        self._fleet(api_client, admissible=False)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result[expected.counter] == 1
        assert sum(result.values()) == 1
        # No refusal ever terminates the story.
        assert result["failed"] == 0
        api_client.fail_story.assert_not_called()

        assert mock_notify.await_count == (1 if expected.admin_alerted else 0)
        if expected.story_action is None:
            api_client.transition_story.assert_not_called()
        else:
            api_client.transition_story.assert_awaited_once_with("story-1", expected.story_action)
        assert self._published_events(redis_client) == (
            [] if expected.owner_event is None else [expected.owner_event]
        )
        # Nothing is re-run: either the platform has no admissible target, or a
        # target would not help.
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", REFUSAL_ROUTING_CASES, ids=lambda case: case.reason.value)
    async def test_only_the_wait_resumes_when_a_target_becomes_admissible(
        self, api_client, redis_client, case
    ):
        """A refusal a server cannot answer must not be resumed by one appearing."""
        from src.tasks.supervisor import supervise_deploying_stories

        expected = case.deploy
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run(case.reason)
        api_client.get_project.return_value = SimpleNamespace(owner_id=555)
        self._fleet(api_client, admissible=True)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        api_client.fail_story.assert_not_called()
        if expected.resumes_when_target_admissible:
            assert result["redispatched"] == 1
            mock_notify.assert_not_called()
            api_client.create_run.assert_called_once()
            published = redis_client.publish_message.call_args
            assert published.args[0] == DEPLOY_QUEUE
            assert published.args[1].head_sha == "b" * 40
            assert published.args[1].story_id == "story-1"
        else:
            assert result["escalated"] == 1
            assert result["redispatched"] == 0
            api_client.create_run.assert_not_called()
            redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wait_that_never_ends_reaches_a_human(self, api_client, redis_client):
        """The bound the engineering wait always had, on the deploy path too.

        Without it a fleet that never recovers leaves the story in DEPLOYING for
        good: polled every tick, visible to nobody.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        started = datetime.now(UTC) - timedelta(minutes=61)
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run(
            run_metadata={"infrastructure_wait_started_at": started.isoformat()}
        )
        self._fleet(api_client, admissible=False)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["escalated"] == 1
        assert result["waiting"] == 0
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        mock_notify.assert_awaited_once()
        api_client.fail_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wait_its_own_server_keeps_refusing_reaches_a_human(
        self, api_client, redis_client
    ):
        """The resume-and-refuse cycle is bounded by the same clock as the wait.

        A project already bound to a host is refused by *that* host — the reuse
        path in `services/langgraph/src/allocations.py` applies admission to the
        server the application sits on. Resuming, though, asks whether *any*
        server is admissible. So a fleet with one healthy host and one broken
        host the project is pinned to satisfies the resume condition on every
        tick and is refused again on every tick.

        This pins that the cycle ends: the elapsed-time bound is checked before
        admissibility, the stamp survives each re-dispatch, and the story reaches
        a human instead of spinning. Without it the deploy would be re-dispatched
        here forever with nobody told — and it would still never fail the story,
        which is exactly what makes an unbounded spin invisible.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        started = datetime.now(UTC) - timedelta(minutes=61)
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run(
            run_metadata={"infrastructure_wait_started_at": started.isoformat()}
        )
        # Another host is perfectly admissible; the project's own host is not.
        self._fleet(api_client, admissible=True)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["escalated"] == 1
        assert result["redispatched"] == 0
        assert result["waiting"] == 0
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        mock_notify.assert_awaited_once()
        api_client.fail_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_wait_keeps_its_start_across_a_redispatch(self, api_client, redis_client):
        """Resuming and being refused again must not reset the bound to zero."""
        from src.tasks.supervisor import supervise_deploying_stories

        started = datetime.now(UTC) - timedelta(minutes=30)
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run(
            run_metadata={"infrastructure_wait_started_at": started.isoformat()}
        )
        self._fleet(api_client, admissible=True)

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        created = api_client.create_run.call_args.args[0]
        assert created["run_metadata"]["infrastructure_wait_started_at"] == started.isoformat()

    @pytest.mark.asyncio
    async def test_a_wait_that_cannot_resume_reaches_a_human(self, api_client, redis_client):
        """A refused run with no head_sha has nothing to wait for."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            run_metadata={},
            result=refused_deploy_result().model_dump(mode="json"),
        )
        self._fleet(api_client, admissible=True)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["escalated"] == 1
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        mock_notify.assert_awaited_once()
        api_client.fail_story.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_misclassified_refusal_reaches_a_human_instead_of_hanging(
        self, api_client, redis_client
    ):
        """The fail-safe: a table that ever called a refusal the project's fault.

        Waiting would hide it forever and failing the story would charge the
        platform's own mistake to the user, so it goes to a human, loudly.
        """
        from shared.allocation_disposition import ALLOCATION_DISPOSITIONS, AttemptDisposition
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = self._refused_run()
        self._fleet(api_client, admissible=False)

        with (
            patch.dict(
                ALLOCATION_DISPOSITIONS,
                {
                    AllocationFailureReason.SERVER_NOT_PROVISIONED: (
                        AttemptDisposition.PRODUCT_FAILURE
                    )
                },
            ),
            patch(
                "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
            ) as mock_notify,
        ):
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["escalated"] == 1
        assert result["waiting"] == 0
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        mock_notify.assert_awaited_once()
        api_client.fail_story.assert_not_called()
