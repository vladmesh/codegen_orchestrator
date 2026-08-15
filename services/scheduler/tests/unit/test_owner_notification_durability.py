"""A terminal story outcome the owner is never told about cannot happen quietly.

The supervisor commits a terminal transition and then publishes to `po:input`.
That order used to be the whole delivery: the publish was an `xadd` with nothing
behind it, and the story it belonged to had just left the only status the loop
scans. A transient failure there — the stream refusing the write, or the
recipient lookup in front of it timing out — lost the message forever, and the
owner's finished product sat there with nobody telling them.

These tests are about the seam that removed that. The message is written on the
QA run *before* the transition commits, so the transition can never be observed
without the message being at least owed; the delivery is then retried from that
record by a sweep that selects on the record instead of on the story's status.
The retry is bounded and its exhaustion calls a human, a recipient with no chat
is refused rather than chased, and a message already delivered is never
published twice.

The world below stores what the API stores, rather than asserting on mock calls,
because every property here is about what the *next* process reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

from _run_routing_factories import _make_repo, _make_run, _make_story, _make_task
import pytest
import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.story import StoryDTO, StoryStatus
from shared.contracts.dto.user import UserDTO
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.tests.allocation_routing_cases import refused_deploy_result

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
OWNER_USER_ID = 100713
OWNER_CHAT_ID = str(900000000 + OWNER_USER_ID)

logger = structlog.get_logger(__name__)


def _project() -> ProjectDTO:
    return ProjectDTO(
        id=UUID(PROJECT_ID),
        initiating_run_id="test-run-1",
        title="Test Project",
        slug="test-project",
        status=ProjectStatus.ACTIVE,
        config={},
        owner_id=OWNER_USER_ID,
        created_at=datetime.now(UTC),
    )


def _owner() -> UserDTO:
    """An owner whose Telegram chat is deliberately nothing like their User.id."""
    return UserDTO(
        id=OWNER_USER_ID,
        telegram_id=int(OWNER_CHAT_ID),
        is_admin=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _qa_run(*, result: dict):
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
                    initiating_run_id="live-run-1",
                    telegram_chat_id="",
                    deployed_url="https://example.com",
                    application_id=42,
                    acceptance_criteria="the bot answers /start",
                    bot_username="palindrome_bot",
                    run_id="qa-1",
                )
            ).model_dump(mode="json"),
        },
        result=result,
    )


def _refused_deploy_run(result):
    """The deploy run a placement refusal leaves behind."""
    return _make_run(
        status=RunStatus.FAILED,
        run_metadata={"head_sha": "b" * 40},
        result=result.model_dump(mode="json"),
    )


#: What each story action the supervisor posts leaves the story in. The World
#: applies these so a test that asks whether the transition committed is asking
#: the question the seam asks — the story's own status — and not a mock's memory
#: of having been called.
_ACTION_RESULT = {
    "complete": StoryStatus.COMPLETED,
    "human-review": StoryStatus.WAITING_HUMAN_REVIEW,
}


class World:
    """What the API and the stream would still be holding after the tick.

    `publish_failures` is how many of the next publishes blow up, which is the
    only way to reproduce the ordering this card is about: the transition has
    already committed, and the publish behind it does not land.

    `transition_failures` and `lost_transition_responses` are the two halves the
    story status has to tell apart: a transition request that never committed,
    and one that committed and lost its answer. They look identical to the
    process that made the request and must not look identical to the recovery.
    """

    def __init__(self, run):
        self.run = run
        self.project = _project()
        self.owner: UserDTO | None = _owner()
        self.story = _make_story(id="story-1", status=StoryStatus.TESTING)
        self.published: list[dict] = []
        self.publish_failures = 0
        self.resolve_failures = 0
        self.story_read_failures = 0
        self.transition_failures = 0
        self.lost_transition_responses = 0
        self.transitions: list[tuple[str, str]] = []
        # Every write that reaches the API, in order, so a record written after
        # the transition it was supposed to protect is visible as such.
        self.journal: list[str] = []
        self.admin_alerts: list[str] = []

    # --- the record, as the next process would read it ---

    @property
    def record(self) -> OwnerNotification | None:
        stored = self.run.run_metadata.get(OWNER_NOTIFICATION_KEY)
        return None if stored is None else OwnerNotification.model_validate(stored)

    def _update_run(self, run_id: str, data: dict) -> None:
        assert run_id == self.run.id
        self.run.run_metadata.update(data["run_metadata"])
        record = self.record
        self.journal.append(f"record:{record.state.value}:{record.attempts}")

    def _transition_story(self, story_id: str, action: str):
        assert story_id == self.story.id
        if self.transition_failures:
            self.transition_failures -= 1
            self.journal.append(f"transition_uncommitted:{action}")
            raise ConnectionError("the API never committed the transition")
        self.transitions.append((story_id, action))
        self.journal.append(f"transition:{action}")
        self.story = self.story.model_copy(update={"status": _ACTION_RESULT[action]})
        if self.lost_transition_responses:
            self.lost_transition_responses -= 1
            self.journal.append(f"transition_response_lost:{action}")
            raise TimeoutError("the transition committed but its answer never arrived")
        return {}

    async def _get_story(self, story_id: str) -> StoryDTO:
        assert story_id == self.story.id
        if self.story_read_failures:
            self.story_read_failures -= 1
            raise TimeoutError("the API did not answer")
        return self.story

    async def _publish_flat(self, queue: str, fields: dict) -> None:
        if self.publish_failures:
            self.publish_failures -= 1
            raise ConnectionError("po:input is unreachable")
        self.journal.append("publish")
        self.published.append(fields)

    async def _get_project(self, project_id: str):
        if self.resolve_failures:
            self.resolve_failures -= 1
            raise TimeoutError("the API did not answer")
        return self.project

    def _owed_runs(self, *, limit: int):
        record = self.record
        if record is not None and record.owed:
            return [self.run][:limit]
        return []


def _reroute(world, api_client, result: dict) -> None:
    """Point the tick at a QA run that ended some other way."""
    world.run = _qa_run(result=result)
    api_client.get_latest_run_by_story.return_value = world.run


@pytest.fixture
def world():
    return World(_qa_run(result={"qa_outcome": QAOutcome.PASSED.value}))


@pytest.fixture
def api_client(world, monkeypatch):
    client = AsyncMock()
    # What the routing loop is given is set by each test, exactly as before:
    # `world.story` is the *stored* story the seam reads back, not the loop's
    # input, and the two are deliberately separate so a test can hold a story in
    # the loop while its stored status says the transition never landed.
    client.get_stories_by_status.return_value = [_make_story(id="story-1", status="testing")]
    client.get_latest_run_by_story.return_value = world.run
    client.get_primary_repository.return_value = _make_repo(bot_username="palindrome_bot")
    client.get_project.side_effect = world._get_project
    client.get_user.side_effect = lambda user_id: world.owner
    client.get_story.side_effect = world._get_story
    client.update_run.side_effect = world._update_run
    client.transition_story.side_effect = world._transition_story
    client.get_tasks_by_story.return_value = []
    client.list_runs_owing_owner_notification.side_effect = world._owed_runs

    async def _alert(message, level="info", **context):
        world.admin_alerts.append(message)

    monkeypatch.setattr("src.tasks.owner_notifications.notify_admins_best_effort", _alert)
    monkeypatch.setattr("src.tasks._recipients.notify_admins_best_effort", _alert)
    monkeypatch.setattr("src.tasks.supervisor.notify_admins_best_effort", _alert)
    return client


@pytest.fixture
def redis_client(world):
    client = AsyncMock()
    client.publish_flat.side_effect = world._publish_flat
    return client


async def _tick(api_client, redis_client):
    """One supervisor cycle, in the order the dispatcher runs these two steps."""
    from src.tasks.owner_notifications import supervise_owed_owner_notifications
    from src.tasks.supervisor import supervise_testing_stories

    counts = await supervise_owed_owner_notifications(api_client, redis_client)
    await supervise_testing_stories(api_client, redis_client)
    return counts


class TestNothingIsPublishedUntilTheTransitionIsProven:
    """The record is written first, so it is not evidence of its own transition.

    Both halves are here, and they are the two ways the same pair of requests
    can come apart. The transition that never committed must not produce a
    message — the owner would be told their story ended while it is still being
    worked on, and believing that is worse than never hearing. The transition
    that committed and lost its answer must produce exactly one, which is the
    loss this card exists to stop. The story's own status is what tells them
    apart; the record cannot, because it was written before either happened.
    """

    @pytest.mark.asyncio
    async def test_a_transition_that_never_committed_publishes_nothing(
        self, world, api_client, redis_client
    ):
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_testing_stories

        world.transition_failures = 1
        with pytest.raises(ConnectionError):
            await supervise_testing_stories(api_client, redis_client)

        # The obligation is on the run, and the story never left TESTING.
        assert world.record.state is OwnerNotificationState.OWED
        assert world.story.status is StoryStatus.TESTING

        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert world.published == []
        assert counts["voided"] == 1
        assert counts["delivered"] == 0
        # Settled, and settled without charging the bound: nothing failed here,
        # there was simply nothing to say yet.
        assert world.record.state is OwnerNotificationState.VOIDED
        assert world.record.attempts == 0

    @pytest.mark.asyncio
    async def test_the_voided_obligation_comes_back_when_the_story_really_ends(
        self, world, api_client, redis_client
    ):
        """Fail-closed is not fail-silent: the ending is owed again, once."""
        from src.tasks.supervisor import supervise_testing_stories

        world.transition_failures = 1
        with pytest.raises(ConnectionError):
            await supervise_testing_stories(api_client, redis_client)

        # The next tick sweeps first, voids it, and then routes the story that
        # is still sitting in TESTING — this time the transition commits.
        await _tick(api_client, redis_client)

        assert world.transitions == [("story-1", "complete")]
        assert world.story.status is StoryStatus.COMPLETED
        assert len(world.published) == 1
        assert world.published[0]["event"] == "story_completed"
        assert world.record.state is OwnerNotificationState.DELIVERED
        # And the good news went out *after* the story was really finished, not
        # from the sweep that ran before routing on a story still in TESTING.
        assert world.journal.index("publish") > world.journal.index("transition:complete")

    @pytest.mark.asyncio
    async def test_a_committed_transition_whose_answer_was_lost_delivers_once(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        world.lost_transition_responses = 1
        with pytest.raises(TimeoutError):
            await supervise_testing_stories(api_client, redis_client)

        # The story is finished; the process that finished it never found out.
        assert world.story.status is StoryStatus.COMPLETED
        assert world.record.state is OwnerNotificationState.OWED
        assert world.published == []

        api_client.get_stories_by_status.return_value = []
        first = await _tick(api_client, redis_client)
        second = await _tick(api_client, redis_client)

        assert first["delivered"] == 1
        assert second["delivered"] == 0
        assert len(world.published) == 1
        assert world.published[0]["event"] == "story_completed"

    @pytest.mark.asyncio
    async def test_a_story_that_cannot_be_read_is_retried_rather_than_voided(
        self, world, api_client, redis_client
    ):
        """A lookup that timed out says nothing about whether the story ended."""
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_testing_stories

        world.publish_failures = 1
        await supervise_testing_stories(api_client, redis_client)
        api_client.get_stories_by_status.return_value = []

        world.story_read_failures = 1
        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert counts["retrying"] == 1
        assert counts["voided"] == 0
        assert world.record.state is OwnerNotificationState.OWED
        assert world.record.attempts == 2

        assert (await supervise_owed_owner_notifications(api_client, redis_client))[
            "delivered"
        ] == 1
        assert len(world.published) == 1


class TestTheRecordComesBeforeTheTransition:
    """AC1: every terminal path owes the message before it commits the transition."""

    @pytest.mark.asyncio
    async def test_a_passed_story_owes_its_message_before_it_completes(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        await supervise_testing_stories(api_client, redis_client)

        assert world.journal[0] == "record:owed:0"
        assert world.journal[1] == "transition:complete"
        assert world.record.state is OwnerNotificationState.DELIVERED

    @pytest.mark.asyncio
    async def test_a_quarantined_story_owes_its_message_before_human_review(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        _reroute(
            world,
            api_client,
            {
                "qa_outcome": QAOutcome.BLOCKED.value,
                "summary": "the bot never answered",
                "blocker": {
                    "category": "deployed_url_unreachable",
                    "attempted": "send /start",
                    "sent": "/start",
                    "received": "nothing",
                },
            },
        )

        await supervise_testing_stories(api_client, redis_client)

        assert world.journal[0] == "record:owed:0"
        assert world.journal[1] == "transition:human-review"
        assert world.record.event == "story_quarantined"

    @pytest.mark.asyncio
    async def test_exhausted_fix_attempts_owe_the_owner_a_message_too(
        self, world, api_client, redis_client
    ):
        """AC6: this path is not admin-only. The owner's story stopped moving.

        Two prior fix tasks carry the same fingerprint, which is what puts this
        QA failure past the configured limit instead of into a third fix.
        """
        from src.tasks.supervisor import supervise_testing_stories

        _reroute(
            world,
            api_client,
            {
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "still broken",
                "failed_checks": [{"name": "weather endpoint", "detail": "500"}],
            },
        )
        api_client.get_tasks_by_story.return_value = [
            _make_task(id=f"task-{index}", failure_metadata={"qa_failure": _prior_failure(index)})
            for index in (1, 2)
        ]

        await supervise_testing_stories(api_client, redis_client)

        assert world.journal[0] == "record:owed:0"
        assert world.journal[1] == "transition:human-review"
        assert world.published[0]["event"] == "story_quarantined"
        assert "specialist" in world.published[0]["text"]
        # The administrators are still told; the owner is no longer the one
        # left out.
        assert any("exhausted" in alert for alert in world.admin_alerts)
        api_client.create_task.assert_not_called()


class TestTheDeployRefusalTakesTheSameSeam:
    """The supervisor's fourth terminal owner notification does not go around it.

    A placement no wait can resolve parks the story for a human and tells its
    owner why. That publish used to sit behind `except Exception: log.warning`,
    which is the same loss with the failure written down as a warning, so it
    goes through the record like the QA paths do.
    """

    @pytest.mark.asyncio
    async def test_an_impossible_placement_owes_before_it_parks_the_story(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import _escalate_refused_deploy

        world.publish_failures = 1
        result = refused_deploy_result(AllocationFailureReason.IMPOSSIBLE_CAPACITY)
        run = _refused_deploy_run(result)
        world.run = run

        await _escalate_refused_deploy(
            api_client,
            redis_client,
            "story-1",
            PROJECT_ID,
            run,
            result,
            tell_owner=True,
            detail="no managed server can hold it",
            log=logger,
        )

        assert world.journal[0] == "record:owed:0"
        assert world.journal[1] == "transition:human-review"
        # And the publish that failed is still owed, not swallowed.
        assert world.record.state is OwnerNotificationState.OWED
        assert world.record.event == "story_impossible_capacity"

    @pytest.mark.asyncio
    async def test_a_refusal_with_nothing_for_the_owner_to_decide_owes_nothing(
        self, world, api_client, redis_client
    ):
        """`tell_owner=False` stays admin-only; the seam does not invent a message."""
        from src.tasks.supervisor import _escalate_refused_deploy

        result = refused_deploy_result(AllocationFailureReason.SERVER_NOT_PROVISIONED)
        run = _refused_deploy_run(result)
        world.run = run

        await _escalate_refused_deploy(
            api_client,
            redis_client,
            "story-1",
            PROJECT_ID,
            run,
            result,
            tell_owner=False,
            detail="the fleet is still installing software",
            log=logger,
        )

        assert world.record is None
        assert world.published == []


class TestTheImpossibleEngineeringPlacementTakesTheSameSeam:
    """The supervisor's fifth terminal owner notification does not go around it.

    An engineering run refused by every managed server parks the task *and its
    parent story* for a human and then tells the owner their request needs an
    operator. That publish used to sit behind `except Exception: log.warning`
    after both transitions had committed, which is the same permanent loss the
    story-level paths had: nothing scans a story in human review, and nothing
    scans a task in `waiting_human_review` looking for a message it owes.
    """

    @staticmethod
    def _engineering_refusal(world, api_client):
        """A failed engineering task whose placement no capacity can satisfy."""
        task = _make_task(id="task-7", story_id="story-1", status="failed")
        run = _make_run(
            id="eng-1",
            type=RunType.ENGINEERING,
            status=RunStatus.FAILED,
            result={
                "engineering_status": "failed",
                "allocation_failure_reason": AllocationFailureReason.IMPOSSIBLE_CAPACITY.value,
            },
        )
        world.run = run
        api_client.get_tasks_by_status.return_value = [task]
        api_client.list_runs.return_value = [run]
        return task

    @pytest.mark.asyncio
    async def test_it_owes_before_the_task_and_its_story_are_parked(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_failed_tasks

        self._engineering_refusal(world, api_client)
        world.publish_failures = 1

        await supervise_failed_tasks(api_client, redis_client)

        assert world.journal[0] == "record:owed:0"
        assert world.journal[1] == "transition:human-review"
        # The publish that failed is owed, not swallowed by the warning.
        assert world.record.state is OwnerNotificationState.OWED
        assert world.record.event == "task_impossible_capacity"
        # And the task is kept, because that is the subject PO answers about.
        assert world.record.task_id == "task-7"

    @pytest.mark.asyncio
    async def test_the_next_cycle_delivers_the_notice_the_publish_lost(
        self, world, api_client, redis_client
    ):
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_failed_tasks

        self._engineering_refusal(world, api_client)
        world.publish_failures = 1
        await supervise_failed_tasks(api_client, redis_client)

        # The task left FAILED and the story left every status the loops scan.
        api_client.get_tasks_by_status.return_value = []
        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert counts["delivered"] == 1
        assert len(world.published) == 1
        assert world.published[0]["event"] == "task_impossible_capacity"
        assert world.published[0]["task_id"] == "task-7"
        assert world.published[0]["story_id"] == "story-1"
        assert world.published[0]["telegram_chat_id"] == OWNER_CHAT_ID

    @pytest.mark.asyncio
    async def test_an_escalation_that_did_not_park_the_story_publishes_nothing(
        self, world, api_client, redis_client
    ):
        """The same proof as the story paths: no transition, no message."""
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_failed_tasks

        self._engineering_refusal(world, api_client)
        world.transition_failures = 1

        with pytest.raises(ConnectionError):
            await supervise_failed_tasks(api_client, redis_client)

        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert counts["voided"] == 1
        assert world.published == []


def _prior_failure(index: int) -> dict:
    """A recorded QA failure with the fingerprint of the one under test."""
    from src.tasks.supervisor import _qa_failure_fingerprint

    return {
        "qa_run_id": f"qa-old-{index}",
        "fingerprint": _qa_failure_fingerprint("still broken", []),
        "fingerprint_attempt": index,
        "fix_attempt": index,
        "summary": "still broken",
        "failed_checks": [],
    }


class TestALostPublishIsPickedUpLater:
    """AC2: the transition committed, the publish did not, the next tick delivers."""

    @pytest.mark.asyncio
    async def test_a_failed_publish_leaves_the_message_owed_and_the_story_complete(
        self, world, api_client, redis_client
    ):
        from src.tasks.supervisor import supervise_testing_stories

        world.publish_failures = 1

        await supervise_testing_stories(api_client, redis_client)

        assert world.transitions == [("story-1", "complete")]
        assert world.published == []
        assert world.record.state is OwnerNotificationState.OWED
        assert world.record.attempts == 1

    @pytest.mark.asyncio
    async def test_the_next_cycle_delivers_what_the_lost_publish_owed(
        self, world, api_client, redis_client
    ):
        """The story is out of TESTING by now — only the record can find it."""
        from src.tasks.supervisor import supervise_testing_stories

        world.publish_failures = 1
        await supervise_testing_stories(api_client, redis_client)

        # The story left TESTING, so the loop that routed it sees nothing.
        api_client.get_stories_by_status.return_value = []
        counts = await _tick(api_client, redis_client)

        assert counts["delivered"] == 1
        assert len(world.published) == 1
        assert world.published[0]["event"] == "story_completed"
        assert world.published[0]["telegram_chat_id"] == OWNER_CHAT_ID
        assert "https://example.com" in world.published[0]["text"]
        assert world.record.state is OwnerNotificationState.DELIVERED

    @pytest.mark.asyncio
    async def test_a_recipient_lookup_that_failed_transiently_is_retried_the_same_way(
        self, world, api_client, redis_client
    ):
        """The lookup sits in the same gap as the publish and fails the same way."""
        from src.tasks.supervisor import supervise_testing_stories

        world.resolve_failures = 1
        await supervise_testing_stories(api_client, redis_client)
        assert world.published == []
        assert world.record.state is OwnerNotificationState.OWED

        api_client.get_stories_by_status.return_value = []
        counts = await _tick(api_client, redis_client)

        assert counts["delivered"] == 1
        assert len(world.published) == 1


class TestTheRetryIsBoundedAndItsEndIsLoud:
    """AC3: three attempts, then an administrator with the identifiers."""

    @pytest.mark.asyncio
    async def test_attempts_run_out_and_a_human_is_called(self, world, api_client, redis_client):
        from src.tasks.supervisor import supervise_testing_stories

        world.publish_failures = 99
        await supervise_testing_stories(api_client, redis_client)
        api_client.get_stories_by_status.return_value = []

        first = await _tick(api_client, redis_client)
        assert first == {
            "delivered": 0,
            "retrying": 1,
            "exhausted": 0,
            "unaddressable": 0,
            "voided": 0,
            "skipped": 0,
        }
        assert world.admin_alerts == []

        second = await _tick(api_client, redis_client)
        assert second["exhausted"] == 1
        assert world.record.state is OwnerNotificationState.ABANDONED
        assert world.record.attempts == 3

        alert = world.admin_alerts[0]
        assert "story-1" in alert
        assert PROJECT_ID in alert
        assert "story_completed" in alert
        assert "qa-1" in alert

    @pytest.mark.asyncio
    async def test_an_abandoned_message_is_not_picked_up_again(
        self, world, api_client, redis_client
    ):
        """'Gave up' is a state, not silence that keeps looking like work."""
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_testing_stories

        world.publish_failures = 99
        await supervise_testing_stories(api_client, redis_client)
        api_client.get_stories_by_status.return_value = []
        await _tick(api_client, redis_client)
        await _tick(api_client, redis_client)

        world.publish_failures = 0
        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert counts == {
            "delivered": 0,
            "retrying": 0,
            "exhausted": 0,
            "unaddressable": 0,
            "voided": 0,
            "skipped": 0,
        }
        assert world.published == []


class TestDeliveredIsDeliveredOnce:
    """AC4: the recovery must not hand the user a second copy of good news."""

    @pytest.mark.asyncio
    async def test_the_sweep_publishes_nothing_after_a_successful_tick(
        self, world, api_client, redis_client
    ):
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_testing_stories

        await supervise_testing_stories(api_client, redis_client)
        assert len(world.published) == 1

        counts = await supervise_owed_owner_notifications(api_client, redis_client)

        assert counts["delivered"] == 0
        assert len(world.published) == 1

    @pytest.mark.asyncio
    async def test_a_repeated_routing_tick_neither_republishes_nor_reopens(
        self, world, api_client, redis_client
    ):
        """A run whose story somehow comes back through routing is already settled."""
        from src.tasks.supervisor import supervise_testing_stories

        await supervise_testing_stories(api_client, redis_client)
        await supervise_testing_stories(api_client, redis_client)

        assert len(world.published) == 1
        assert world.record.state is OwnerNotificationState.DELIVERED
        assert world.record.attempts == 1


class TestAnUnaddressableOwnerIsRefusedNotChased:
    """AC5: no chat id is an answer, and answers are not retried."""

    @pytest.mark.asyncio
    async def test_a_project_without_an_owner_settles_immediately(
        self, world, api_client, redis_client
    ):
        from src.tasks.owner_notifications import supervise_owed_owner_notifications
        from src.tasks.supervisor import supervise_testing_stories

        world.owner = None

        await supervise_testing_stories(api_client, redis_client)

        assert world.published == []
        assert world.record.state is OwnerNotificationState.UNADDRESSABLE
        assert "owner user not found" in world.record.detail
        # And nothing keeps asking: the record is out of the selection.
        assert await supervise_owed_owner_notifications(api_client, redis_client) == {
            "delivered": 0,
            "retrying": 0,
            "exhausted": 0,
            "unaddressable": 0,
            "voided": 0,
            "skipped": 0,
        }

    @pytest.mark.asyncio
    async def test_it_is_told_apart_from_a_transient_failure(self, world, api_client, redis_client):
        """Same tick, two failures, two different endings."""
        from src.tasks.supervisor import supervise_testing_stories

        world.resolve_failures = 1
        await supervise_testing_stories(api_client, redis_client)
        transient = world.record

        world.run.run_metadata.pop(OWNER_NOTIFICATION_KEY)
        world.owner = None
        await supervise_testing_stories(api_client, redis_client)

        assert transient.state is OwnerNotificationState.OWED
        assert world.record.state is OwnerNotificationState.UNADDRESSABLE
