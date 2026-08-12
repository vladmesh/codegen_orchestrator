"""The QA handoff runs through the durable grant, and delivery does not wait for it.

These cover the two seams between the supervisor and the temporary-access
sweep: a deploy that succeeded hands QA over by recording a grant instead of
publishing straight to the queue, and a story in TESTING is routed on what QA
said about the product, with the state of the borrowed identity left to the
sweep that owns it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

from _run_routing_factories import _make_repo, _make_run, _make_story
import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID, TEST_IDENTITY_ENV_KEY
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.qa_handoff import (
    QA_DISPATCHED_AT_KEY,
    QA_HANDOFF_KEY,
    QAHandoffPlan,
    TemporaryAccessRequest,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.dto.user import UserDTO
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAMessage, QAOutcome
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
        # The request carries the project as a UUID (the column's type), the
        # stored record as the string the API answers with.
        project_id=str(payload.project_id),
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


def _resolved_user(user_id: int) -> UserDTO:
    """A user whose Telegram chat id is deliberately nothing like their User.id."""
    return UserDTO(
        id=user_id,
        telegram_id=900000000 + user_id,
        is_admin=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_primary_repository.return_value = _make_repo(bot_username="palindrome_bot")
    client.get_live_temporary_access_grant_for_run.return_value = None
    # No deploy has been made for any run id yet, the grant's included.
    client.get_run_if_missing_returns_none.return_value = None
    client.create_temporary_access_grant.side_effect = _stored_grant
    client.transition_story.return_value = {}
    client.get_user.side_effect = _resolved_user
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
            "telegram_chat_id": "",
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
        run_metadata={
            "application_id": 42,
            QA_HANDOFF_KEY: QAHandoffPlan(
                qa_message=QAMessage(
                    story_id="story-1",
                    project_id=PROJECT_ID,
                    telegram_chat_id="",
                    deployed_url="https://example.com",
                    application_id=42,
                    acceptance_criteria="the bot answers /start",
                    bot_username="palindrome_bot",
                    run_id="qa-1",
                ),
                access=TemporaryAccessRequest(
                    env_key=TEST_IDENTITY_ENV_KEY,
                    subject=str(QA_TEST_TELEGRAM_ID),
                    head_sha=HEAD_SHA,
                ),
            ).model_dump(mode="json"),
        },
        result={"qa_outcome": QAOutcome.PASSED.value},
    )


def _po_events(redis_client) -> list[dict]:
    return [c.args[1] for c in redis_client.publish_flat.call_args_list]


class TestDeliveryDoesNotWaitForTheCleanup:
    """A product QA passed is handed over now; the identity is handed back later."""

    @pytest.mark.asyncio
    async def test_passed_qa_completes_while_the_grant_is_still_live(
        self, api_client, redis_client
    ):
        """The card's whole point: a revoke still being worked on holds nothing up."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _passed_qa_run()
        api_client.get_live_temporary_access_grant_for_run.return_value = _live_grant()
        api_client.get_project.return_value = _project("42")

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 1, "redispatched": 0, "failed": 0, "recovered": 0}
        api_client.transition_story.assert_awaited_once_with("story-1", "complete")
        # And not because the grant happened to look finished: the routing does
        # not ask about it at all. A live grant is set above precisely so that
        # reading it could only produce the old wait.
        api_client.get_live_temporary_access_grant_for_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_owner_is_told_where_their_bot_is_in_the_same_tick(
        self, api_client, redis_client
    ):
        """Completion the user never hears about is not delivery."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _passed_qa_run()
        api_client.get_live_temporary_access_grant_for_run.return_value = _live_grant()
        api_client.get_project.return_value = _project("42")

        await supervise_testing_stories(api_client, redis_client)

        events = _po_events(redis_client)
        assert len(events) == 1
        assert events[0]["event"] == "story_completed"
        assert events[0]["story_id"] == "story-1"
        assert "https://example.com" in events[0]["text"]
        assert "palindrome_bot" in events[0]["text"]
        # The owner's Telegram chat, not the internal user id.
        assert events[0]["telegram_chat_id"] == str(900000000 + 100713)

    @pytest.mark.asyncio
    async def test_a_run_blocked_on_its_cleanup_still_ends_in_human_review(
        self, api_client, redis_client
    ):
        """A QA run whose only verdict is a cleanup failure verified no product.

        The sweep writes this outcome when it gives up, and a story still in
        TESTING behind it has nothing that says the bot works — so it stops the
        application and asks for a human. What changed is that a story whose QA
        did pass has already been completed by then and is never seen here.
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
        api_client.stop_application.assert_awaited_once_with(42)
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")

    @pytest.mark.asyncio
    async def test_an_escalated_grant_does_not_hold_a_passed_run_back(
        self, api_client, redis_client
    ):
        """The sweep has given up on the access and the product is still delivered.

        The leftover test identity is an incident the sweep reports to an
        administrator. Reading it as a verdict on the product is what this card
        removed.
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
        api_client.get_project.return_value = _project("42")

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["completed"] == 1
        api_client.transition_story.assert_awaited_once_with("story-1", "complete")
        api_client.stop_application.assert_not_called()


class TestTheHandoffSurvivesARestart:
    """The handoff is recoverable from the QA run, not from the process that planned it.

    Everything it needs is written with the run before the story leaves
    DEPLOYING. A process that dies anywhere after that leaves a queued QA run
    carrying its own plan, and any later tick finishes it.
    """

    def _queued_qa_run(self, plan: QAHandoffPlan, *, age_minutes: int = 30, **metadata):
        return _make_run(
            id="qa-deploy-1",
            type=RunType.QA,
            status=RunStatus.QUEUED,
            result=None,
            created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
            run_metadata={
                "application_id": 42,
                QA_HANDOFF_KEY: plan.model_dump(mode="json"),
                **metadata,
            },
        )

    def _plan(self, *, needs_access: bool) -> QAHandoffPlan:
        return QAHandoffPlan(
            qa_message=QAMessage(
                story_id="story-1",
                project_id=PROJECT_ID,
                telegram_chat_id="",
                deployed_url="https://example.com",
                application_id=42,
                acceptance_criteria="the bot answers /start",
                bot_username="palindrome_bot",
                run_id="qa-deploy-1",
            ),
            access=TemporaryAccessRequest(
                env_key=TEST_IDENTITY_ENV_KEY,
                subject=str(QA_TEST_TELEGRAM_ID),
                head_sha=HEAD_SHA,
            )
            if needs_access
            else None,
        )

    @pytest.mark.asyncio
    async def test_a_death_before_the_grant_is_recorded_is_picked_up_later(
        self, api_client, redis_client
    ):
        """The window the deploy supervisor no longer covers and no grant exists for."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = self._queued_qa_run(
            self._plan(needs_access=True)
        )
        api_client.temporary_access_grant_exists_for_run.return_value = False

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["recovered"] == 1
        payload = api_client.create_temporary_access_grant.call_args.args[0]
        assert payload.qa_run_id == "qa-deploy-1"
        assert payload.head_sha == HEAD_SHA
        assert _published(redis_client, DEPLOY_QUEUE)[0].env_overrides == {
            TEST_IDENTITY_ENV_KEY: str(QA_TEST_TELEGRAM_ID)
        }

    @pytest.mark.asyncio
    async def test_a_death_before_the_qa_publish_is_picked_up_later(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = self._queued_qa_run(
            self._plan(needs_access=False)
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["recovered"] == 1
        assert _published(redis_client, QA_QUEUE)[0].run_id == "qa-deploy-1"

    @pytest.mark.asyncio
    async def test_a_handoff_that_already_recorded_its_grant_is_left_alone(
        self, api_client, redis_client
    ):
        """From the grant onwards the temporary-access sweep owns the run."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = self._queued_qa_run(
            self._plan(needs_access=True)
        )
        api_client.temporary_access_grant_exists_for_run.return_value = True

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["recovered"] == 0
        api_client.create_temporary_access_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_published_handoff_is_not_published_twice(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = self._queued_qa_run(
            self._plan(needs_access=False),
            **{QA_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat()},
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["recovered"] == 0
        assert _published(redis_client, QA_QUEUE) == []

    @pytest.mark.asyncio
    async def test_a_handoff_still_in_progress_is_not_taken_over(self, api_client, redis_client):
        """A run created seconds ago is being worked on, not abandoned."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = self._queued_qa_run(
            self._plan(needs_access=False), age_minutes=0
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["recovered"] == 0
        assert _published(redis_client, QA_QUEUE) == []


class TestTheQARunIsWrittenBeforeTheStoryMoves:
    """Ordering is what makes the crash windows recoverable in both directions."""

    @pytest.mark.asyncio
    async def test_the_run_carrying_the_plan_exists_before_the_transition(
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

        order = []
        api_client.create_run_if_absent.side_effect = lambda data: order.append("run")
        api_client.transition_story.side_effect = lambda *a: order.append("transition")
        api_client.create_temporary_access_grant.side_effect = lambda payload: (
            order.append("grant") or _stored_grant(payload)
        )

        await supervise_deploying_stories(api_client, redis_client)

        assert order == ["run", "transition", "grant"]

    @pytest.mark.asyncio
    async def test_repeating_the_handoff_lands_on_the_same_run_and_grant(
        self, api_client, redis_client
    ):
        """A crash before the transition leaves the story for the deploy supervisor.

        It runs the handoff again, and the ids derived from the deploy run are
        what stop that from becoming a second QA run and a second grant.
        """
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _deploy_success_run(
            test_identity_slot=True
        )
        api_client.get_project.return_value = _project("42")

        await supervise_deploying_stories(api_client, redis_client)
        await supervise_deploying_stories(api_client, redis_client)

        run_ids = {c.args[0]["id"] for c in api_client.create_run_if_absent.call_args_list}
        grant_ids = {c.args[0].id for c in api_client.create_temporary_access_grant.call_args_list}
        assert len(run_ids) == 1
        assert len(grant_ids) == 1
