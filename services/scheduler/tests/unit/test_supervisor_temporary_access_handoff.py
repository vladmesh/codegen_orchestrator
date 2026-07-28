"""The QA handoff runs through the durable grant, and stories wait for it.

These cover the two seams between the supervisor and the temporary-access
sweep: a deploy that succeeded hands QA over by recording a grant instead of
publishing straight to the queue, and a story in TESTING does not reach a
terminal outcome while its QA run still holds access on the deployed bot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

from _run_routing_factories import _make_repo, _make_run, _make_story
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID, TEST_IDENTITY_ENV_KEY
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.queues import DEPLOY_QUEUE, QA_QUEUE

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
HEAD_SHA = "b" * 40


def _project(audience: str | None) -> ProjectDTO:
    config = {}
    if audience is not None:
        config["bot_access"] = {"mode": "custom", "allowed_telegram_ids": audience}
    return ProjectDTO(
        id=UUID(PROJECT_ID),
        title="Test Project",
        slug="test-project",
        status=ProjectStatus.ACTIVE,
        config=config,
        owner_id=100713,
        created_at=datetime.now(UTC),
    )


def _deploy_success_run(*, test_identity_slot: bool, head_sha: str | None = HEAD_SHA):
    return _make_run(
        result={
            "deploy_outcome": DeployOutcome.SUCCESS.value,
            "deployed_url": "https://example.com",
            "application_id": 42,
            "test_identity_slot": test_identity_slot,
        },
        run_metadata={"head_sha": head_sha} if head_sha else {},
    )


def _stored_grant(payload) -> TemporaryAccessGrantDTO:
    return TemporaryAccessGrantDTO(
        id=payload.id,
        project_id=payload.project_id,
        env_key=payload.env_key,
        subject=payload.subject,
        head_sha=payload.head_sha,
        qa_run_id=payload.qa_run_id,
        grant_run_id=payload.grant_run_id,
        qa_message=payload.qa_message,
        status=TemporaryAccessStatus.GRANTING,
        granted_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_primary_repository.return_value = _make_repo(bot_username="palindrome_bot")
    client.get_live_temporary_access_grant_for_run.return_value = None
    client.create_temporary_access_grant.side_effect = _stored_grant
    client.transition_story.return_value = {}
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    client._redis = AsyncMock()
    client._redis.get = AsyncMock(return_value=None)
    return client


def _published(redis_client, queue) -> list:
    return [c.args[1] for c in redis_client.publish_message.call_args_list if c.args[0] == queue]


class TestHandoffThroughTheGrant:
    """A private bot's QA run starts from a recorded grant, not from the queue."""

    @pytest.mark.asyncio
    async def test_private_bot_records_a_grant_and_holds_the_qa_message(
        self, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=True
        )
        api_client.get_project.return_value = _project("42")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.transition_story.assert_awaited_once_with("story-1", "test")

        payload = api_client.create_temporary_access_grant.call_args.args[0]
        assert payload.env_key == TEST_IDENTITY_ENV_KEY
        assert payload.subject == str(QA_TEST_TELEGRAM_ID)
        assert payload.head_sha == HEAD_SHA
        # The QA run exists and is named by the record, but nothing has been
        # published to QA: the access has not been confirmed yet.
        assert payload.qa_message.run_id == payload.qa_run_id
        assert _published(redis_client, QA_QUEUE) == []

        deploys = _published(redis_client, DEPLOY_QUEUE)
        assert len(deploys) == 1
        assert deploys[0].env_overrides == {TEST_IDENTITY_ENV_KEY: str(QA_TEST_TELEGRAM_ID)}
        assert deploys[0].head_sha == HEAD_SHA

    @pytest.mark.asyncio
    async def test_public_bot_needs_no_grant(self, api_client, redis_client):
        """An empty audience admits everyone, so there is nothing to hand over."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=True
        )
        api_client.get_project.return_value = _project("")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.create_temporary_access_grant.assert_not_called()
        assert len(_published(redis_client, QA_QUEUE)) == 1

    @pytest.mark.asyncio
    async def test_audience_that_already_lists_qa_needs_no_grant(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=True
        )
        api_client.get_project.return_value = _project(f"42,{QA_TEST_TELEGRAM_ID}")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.create_temporary_access_grant.assert_not_called()
        assert len(_published(redis_client, QA_QUEUE)) == 1

    @pytest.mark.asyncio
    async def test_commit_without_the_slot_is_not_granted_anything(self, api_client, redis_client):
        """Deploying a value the generated repository never declared would fail."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=False
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.create_temporary_access_grant.assert_not_called()
        api_client.get_project.assert_not_called()
        assert len(_published(redis_client, QA_QUEUE)) == 1

    @pytest.mark.asyncio
    async def test_grant_without_a_known_commit_fails_the_story(self, api_client, redis_client):
        """The revoke redeploys the granted commit, so an unknown one is fatal."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=True, head_sha=None
        )
        api_client.get_project.return_value = _project("42")

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_awaited_once_with("story-1")
        api_client.transition_story.assert_not_called()
        api_client.create_temporary_access_grant.assert_not_called()
        assert _published(redis_client, QA_QUEUE) == []


def _live_grant(**overrides) -> TemporaryAccessGrantDTO:
    defaults = {
        "id": "tempaccess-1",
        "project_id": PROJECT_ID,
        "env_key": TEST_IDENTITY_ENV_KEY,
        "subject": str(QA_TEST_TELEGRAM_ID),
        "head_sha": HEAD_SHA,
        "qa_run_id": "qa-1",
        "grant_run_id": "deploy-grant-1",
        "qa_message": {
            "story_id": "story-1",
            "project_id": PROJECT_ID,
            "user_id": "",
            "deployed_url": "https://example.com",
            "application_id": 42,
            "acceptance_criteria": "the bot answers /start",
            "run_id": "qa-1",
        },
        "status": TemporaryAccessStatus.REVOKING,
        "granted_at": datetime.now(UTC),
        "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL,
        "revoke_run_id": "deploy-revoke-1",
        "revoke_attempts": 1,
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return TemporaryAccessGrantDTO(**defaults)


def _passed_qa_run():
    return _make_run(
        id="qa-1",
        type=RunType.QA,
        status=RunStatus.COMPLETED,
        run_metadata={"application_id": 42},
        result={"qa_outcome": QAOutcome.PASSED.value},
    )


class TestStoriesWaitForTheAccessToGoBack:
    """A story is not finished while its bot still admits the test identity."""

    @pytest.mark.asyncio
    async def test_passed_qa_does_not_complete_while_the_grant_is_live(
        self, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _passed_qa_run()
        api_client.get_live_temporary_access_grant_for_run.return_value = _live_grant()

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {
            "completed": 0,
            "redispatched": 0,
            "failed": 0,
            "waiting_for_access": 1,
        }
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_passed_qa_completes_once_the_access_is_back(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _passed_qa_run()
        api_client.get_live_temporary_access_grant_for_run.return_value = None

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["completed"] == 1
        api_client.transition_story.assert_awaited_once_with("story-1", "complete")

    @pytest.mark.asyncio
    async def test_access_that_cannot_be_revoked_ends_in_human_review(
        self, api_client, redis_client
    ):
        """The sweep failed the run and gave up on quiet retries; that is visible.

        The story must not read as a success next to a bot that still admits the
        test identity, so it stops the application and asks for a human.
        """
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            status=RunStatus.FAILED,
            run_metadata={"application_id": 42},
            result={
                "qa_outcome": QAOutcome.BLOCKED.value,
                "summary": "temporary test access could not be revoked",
                "blocker": {
                    "category": "qa_cleanup_failed",
                    "attempted": "revoke temporary access",
                    "sent": "cleared value",
                    "received": "deploy failed",
                },
            },
        )
        api_client.get_live_temporary_access_grant_for_run.return_value = _live_grant(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            revoke_attempts=3,
            escalated_at=datetime.now(UTC),
            last_error="revoke deploy deploy-revoke-1 ended failed (give_up)",
        )
        api_client.get_project.return_value = _project("42")

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        assert result["waiting_for_access"] == 0
        api_client.stop_application.assert_awaited_once_with(42)
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")

    @pytest.mark.asyncio
    async def test_an_escalation_stamp_alone_does_not_publish_a_passed_story(
        self, api_client, redis_client
    ):
        """The stamp landed, the QA run's failure did not — the story still waits.

        This is the crash window between the two writes the sweep makes. Routing
        on the run as it stands would complete a story whose bot may still admit
        the test identity, because the run still says the QA passed.
        """
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _passed_qa_run()
        api_client.get_live_temporary_access_grant_for_run.return_value = _live_grant(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            revoke_attempts=3,
            escalated_at=datetime.now(UTC),
            last_error="revoke deploy deploy-revoke-1 ended failed (give_up)",
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["waiting_for_access"] == 1
        assert result["completed"] == 0
        api_client.transition_story.assert_not_called()
