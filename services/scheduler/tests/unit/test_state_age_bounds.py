"""Every bounded wait ends, once, with a typed reason and a told owner.

One test per bounded state with an aged record, and one per state just under the
bound that must be left alone. The four states are deliberately exercised
through the same public watchdog: there is one map and one sweep, so a state
that needed its own entry point would be visible here as a second call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_repo, _make_run, _make_story
import pytest
import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.supervisor.deploy import (
    _handle_deploy_waiting_user_secret,
    owe_user_secret_request,
)
from src.tasks.supervisor.state_age import (
    STATE_AGE_BOUND_REASON,
    STATE_AGE_BOUNDS,
    USER_SECRET_REQUEST_UNDELIVERED_REASON,
    supervise_state_age_bounds,
)

DEPLOY_BOUND_MINUTES = 30
QA_BOUND_MINUTES = 60
PR_REVIEW_BOUND_MINUTES = 220
USER_SECRET_BOUND_MINUTES = 1440

logger = structlog.get_logger(__name__)


def _ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _stories_by_status(status: str, stories: list) -> object:
    """A `get_stories_by_status` double that answers only the status under test."""

    async def answer(requested):
        return stories if requested == status else []

    return answer


def _pull_request(*, updated_at: datetime, state: str = "open", merged_at=None) -> dict:
    return {
        "number": 42,
        "state": state,
        "merged_at": merged_at,
        "auto_merge": None,
        "mergeable_state": "clean",
        "updated_at": updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at": updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head": {"sha": "a" * 40},
    }


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_stories_by_status.side_effect = _stories_by_status("none", [])
    return client


@pytest.fixture
def redis_client():
    return AsyncMock()


def _patches():
    return (
        patch("src.tasks.supervisor.state_age.notify_admins_best_effort", new_callable=AsyncMock),
        patch("src.tasks.supervisor.state_age.deliver_owed_notification", new_callable=AsyncMock),
        patch(
            "src.tasks.supervisor.state_age.owe_story_owner_notification", new_callable=AsyncMock
        ),
        patch("src.tasks.supervisor.state_age.GitHubAppClient"),
    )


async def _run_watchdog(api_client, redis_client, *, pull_request: dict | None = None):
    """Run the sweep with its notification seam and GitHub reads doubled."""
    notify_p, deliver_p, owe_p, github_p = _patches()
    with notify_p as notify, deliver_p as deliver, owe_p as owe, github_p as github_cls:
        github = AsyncMock()
        github_cls.return_value = github
        if pull_request is not None:
            github.get_pull_request.return_value = pull_request
        counts = await supervise_state_age_bounds(api_client, redis_client)
        return counts, owe, deliver, notify


def test_every_bound_is_one_map_entry_with_its_own_configuration_key():
    """The map is the contract: one entry per state, each naming its own key."""
    assert [bound.status for bound in STATE_AGE_BOUNDS] == [
        StoryStatus.DEPLOYING,
        StoryStatus.TESTING,
        StoryStatus.PR_REVIEW,
        StoryStatus.WAITING_USER_SECRET,
    ]
    keys = [bound.config_key for bound in STATE_AGE_BOUNDS]
    assert len(set(keys)) == len(keys)
    assert all(key.startswith("supervisor.") for key in keys)


def _assert_parked_once(api_client, owe, deliver, notify, *, status: str, anchor: str):
    reason = api_client.update_story.await_args.args[1]["quarantine_reason"]
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == status
    assert reason["anchor"] == anchor
    assert reason["threshold_minutes"] > 0
    owe.assert_awaited_once()
    assert owe.await_args.kwargs["event"] is OwnerNotificationEvent.STORY_BLOCKED
    assert owe.await_args.kwargs["terminal_status"] is StoryStatus.WAITING_HUMAN_REVIEW
    api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
    api_client.fail_story.assert_not_awaited()
    deliver.assert_awaited_once()
    notify.assert_awaited_once()


# --- deploying ------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploying_with_an_old_running_deploy_run_is_parked_once(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.RUNNING, created_at=_ago(DEPLOY_BOUND_MINUTES + 5)
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 1, "failed": 0}
    _assert_parked_once(
        api_client,
        owe,
        deliver,
        notify,
        status="deploying",
        anchor="deploy_run_created_at",
    )


@pytest.mark.asyncio
async def test_deploying_just_under_the_bound_is_left_alone(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.RUNNING, created_at=_ago(DEPLOY_BOUND_MINUTES - 1)
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    api_client.transition_story.assert_not_awaited()
    api_client.update_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_story_whose_deploy_run_has_finished_is_not_the_watchdogs_business(
    api_client, redis_client
):
    """A terminal run is an outcome the deploy supervisor routes on this tick."""
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.COMPLETED,
        created_at=_ago(DEPLOY_BOUND_MINUTES * 10),
        result={"deploy_outcome": "success"},
    )

    counts, owe, _deliver, _notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_redispatched_deploy_restarts_the_bound(api_client, redis_client):
    """Progress is a new Run, and the bound is measured from that Run."""
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying", created_at=_ago(600))]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        id="deploy-2", status=RunStatus.QUEUED, created_at=_ago(1)
    )

    counts, owe, _deliver, _notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()


# --- testing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_testing_with_an_old_running_qa_run_is_parked_once(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.TESTING, [_make_story(status="testing")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        id="qa-1",
        type=RunType.QA,
        status=RunStatus.RUNNING,
        created_at=_ago(QA_BOUND_MINUTES + 5),
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 1, "failed": 0}
    _assert_parked_once(
        api_client, owe, deliver, notify, status="testing", anchor="qa_run_created_at"
    )


@pytest.mark.asyncio
async def test_testing_just_under_the_bound_is_left_alone(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.TESTING, [_make_story(status="testing")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        id="qa-1",
        type=RunType.QA,
        status=RunStatus.RUNNING,
        created_at=_ago(QA_BOUND_MINUTES - 1),
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    api_client.transition_story.assert_not_awaited()


# --- pr_review ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_unmerged_pull_request_that_stopped_moving_is_parked_once(
    api_client, redis_client
):
    """The backstop under the auto-merge fallback, whatever left the PR waiting.

    Card codegen-orchestrator-1323 rests on GitHub's own `mergeable_state`. If
    that reading is ever wrong the story waits on a state that will not change,
    and this is the bound that turns it into a parked story with a told owner.
    """
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()

    counts, owe, deliver, notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES + 30)),
    )

    assert counts == {"parked": 1, "failed": 0}
    _assert_parked_once(
        api_client,
        owe,
        deliver,
        notify,
        status="pr_review",
        anchor="github_pull_request_updated_at",
    )


@pytest.mark.asyncio
async def test_a_pull_request_that_moved_recently_is_left_alone(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()

    counts, owe, deliver, notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES - 10)),
    )

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    api_client.transition_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_merged_story_inside_the_image_publication_window_is_not_parked(
    api_client, redis_client
):
    """The 900 s image bound owns that wait and always ends it; this one waits."""
    merged = _ago(10)
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()

    counts, owe, _deliver, _notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(
            updated_at=merged,
            state="closed",
            merged_at=merged.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_pull_request_never_ends_a_wait(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()

    notify_p, deliver_p, owe_p, github_p = _patches()
    with notify_p, deliver_p, owe_p as owe, github_p as github_cls:
        github = AsyncMock()
        github_cls.return_value = github
        github.get_pull_request.side_effect = RuntimeError("GitHub is unreachable")
        counts = await supervise_state_age_bounds(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()


# --- waiting_user_secret --------------------------------------------------
#
# The clock of this wait starts only when the ask is delivered to the owner, and
# the ask is a durable owner-notification record on the deploy Run that found the
# secrets missing. These tests drive the real seam — owe, deliver, the recovery
# sweep — against a small stateful double of the API and Redis, so each state of
# that record is reached the way production reaches it, not asserted into place.


class _SecretWait:
    """The API and Redis a secret wait touches, holding their state across ticks."""

    RUN_ID = "deploy-secret-source"

    def __init__(
        self,
        *,
        story_status: str = "waiting_user_secret",
        consumer_wrote_at: datetime,
        owner_telegram_id: int | None = 900000555,
        record: dict | None = None,
    ):
        self.story_status = story_status
        self.consumer_wrote_at = consumer_wrote_at
        self.run_metadata: dict = {} if record is None else {OWNER_NOTIFICATION_KEY: record}
        self.quarantine_reason: dict | None = None
        self.published: list[dict] = []
        self.publish_fails = False

        api = AsyncMock()
        api.get_stories_by_status.side_effect = self._stories
        api.get_latest_run_by_story.side_effect = self._latest_run
        api.update_run.side_effect = self._update_run
        api.get_story.side_effect = self._story
        api.update_story.side_effect = self._update_story
        api.fail_story.side_effect = self._fail_story
        api.wait_user_secret_story.side_effect = self._wait_user_secret_story
        api.list_runs_owing_owner_notification.side_effect = self._runs_owing
        api.list_stories_owing_owner_notification.return_value = []
        api.get_project.return_value = SimpleNamespace(owner_id=555)
        api.get_user.return_value = SimpleNamespace(telegram_id=owner_telegram_id)
        self.api = api

        redis = AsyncMock()
        redis.publish_flat.side_effect = self._publish
        self.redis = redis

    # -- the API, as far as a secret wait reads and writes it --

    def _story_dto(self):
        return _make_story(status=self.story_status, quarantine_reason=self.quarantine_reason)

    async def _stories(self, status):
        return [self._story_dto()] if status == self.story_status else []

    def run(self):
        return _make_run(
            id=self.RUN_ID,
            status=RunStatus.COMPLETED,
            created_at=self.consumer_wrote_at - timedelta(minutes=5),
            updated_at=self.consumer_wrote_at,
            run_metadata=dict(self.run_metadata),
            result={
                "deploy_outcome": "waiting_for_user_secret",
                "missing_user_secrets": [{"key": "STRIPE_KEY", "description": "Stripe secret key"}],
            },
        )

    async def _latest_run(self, story_id, run_type=None):
        return self.run()

    async def _update_run(self, run_id, data):
        self.run_metadata = {**self.run_metadata, **data.get("run_metadata", {})}

    async def _story(self, story_id):
        return self._story_dto()

    async def _update_story(self, story_id, data):
        self.quarantine_reason = data.get("quarantine_reason", self.quarantine_reason)
        return self._story_dto()

    async def _fail_story(self, story_id):
        self.story_status = "failed"
        return self._story_dto()

    async def _wait_user_secret_story(self, story_id):
        self.story_status = "waiting_user_secret"
        return self._story_dto()

    async def _runs_owing(self, *, limit):
        record = self.record()
        return [self.run()] if record is not None and record.owed else []

    async def _publish(self, stream, fields):
        if self.publish_fails:
            raise ConnectionError("po:input is unavailable")
        self.published.append(fields)

    # -- what the tests read --

    def record(self) -> OwnerNotification | None:
        stored = self.run_metadata.get(OWNER_NOTIFICATION_KEY)
        return None if stored is None else OwnerNotification.model_validate(stored)

    def set_record(self, **update) -> None:
        record = self.record().model_copy(update=update)
        self.run_metadata[OWNER_NOTIFICATION_KEY] = record.model_dump(mode="json")

    async def enter_the_wait(self) -> None:
        """What `supervise_deploying_stories` does with this Run's outcome."""
        await _handle_deploy_waiting_user_secret(
            self.api,
            self.redis,
            "story-1",
            "00000000-0000-0000-0000-000000000001",
            self.run(),
            logger.bind(story_id="story-1"),
        )

    async def sweep_owed_notifications(self) -> None:
        await supervise_owed_owner_notifications(self.api, self.redis)


def _ask_record(state: OwnerNotificationState, **fields) -> dict:
    """An ask record in one settled or owed state, as the seam would have left it."""
    return OwnerNotification(
        event=OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
        text="Ask the user for STRIPE_KEY.",
        story_id="story-1",
        project_id="00000000-0000-0000-0000-000000000001",
        terminal_status=StoryStatus.WAITING_USER_SECRET,
        state=state,
        **fields,
    ).model_dump(mode="json")


def _asks_published(world: _SecretWait) -> int:
    return sum(1 for fields in world.published if fields["event"] == "story_waiting_user_secret")


@pytest.fixture(autouse=True)
def _quiet_recipient_alerts():
    """An unaddressable owner alerts administrators from the recipient lookup."""
    with patch("src.tasks._recipients.notify_admins_best_effort", new_callable=AsyncMock):
        yield


@pytest.mark.asyncio
async def test_a_request_delivered_just_now_is_not_expired_by_an_old_run():
    """Gap one: the consumer wrote the run long ago; the owner was told a minute ago.

    `owed_at` is old too — owing is not telling, so it must not be the anchor.
    """
    world = _SecretWait(
        consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 3),
        record=_ask_record(
            OwnerNotificationState.DELIVERED,
            owed_at=_ago(USER_SECRET_BOUND_MINUTES * 3),
            delivered_at=_ago(1),
            attempts=2,
        ),
    )

    counts, owe, _deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0}
    assert world.story_status == "waiting_user_secret"
    owe.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_old_delivered_request_fails_the_story_once_even_if_the_run_is_recent():
    """Gap two: the bound measures from the delivery, not from the run."""
    world = _SecretWait(
        consumer_wrote_at=_ago(2),
        record=_ask_record(
            OwnerNotificationState.DELIVERED,
            owed_at=_ago(USER_SECRET_BOUND_MINUTES + 61),
            delivered_at=_ago(USER_SECRET_BOUND_MINUTES + 60),
            attempts=1,
        ),
    )

    counts, owe, deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 1}
    reason = world.quarantine_reason
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == "waiting_user_secret"
    assert reason["anchor"] == "user_secret_request_delivered_at"
    assert reason["ending"] == "fail"
    owe.assert_awaited_once()
    assert owe.await_args.kwargs["event"] is OwnerNotificationEvent.STORY_FAILED
    assert owe.await_args.kwargs["terminal_status"] is StoryStatus.FAILED
    world.api.fail_story.assert_awaited_once_with("story-1")
    world.api.transition_story.assert_not_awaited()
    deliver.assert_awaited_once()
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_delivered_request_just_under_the_bound_is_left_alone():
    world = _SecretWait(
        consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 2),
        record=_ask_record(
            OwnerNotificationState.DELIVERED,
            owed_at=_ago(USER_SECRET_BOUND_MINUTES - 59),
            delivered_at=_ago(USER_SECRET_BOUND_MINUTES - 60),
            attempts=1,
        ),
    )

    counts, owe, deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    world.api.fail_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_entering_the_wait_owes_the_ask_before_the_transition_and_delivers_it():
    """The seam's mandated order: owe, transition, deliver."""
    world = _SecretWait(story_status="deploying", consumer_wrote_at=_ago(30))
    trace: list[tuple[str, ...]] = []
    write_run, transition = world._update_run, world._wait_user_secret_story

    async def traced_write(run_id, data):
        trace.append(("record", data["run_metadata"][OWNER_NOTIFICATION_KEY]["state"]))
        await write_run(run_id, data)

    async def traced_transition(story_id):
        trace.append(("transition",))
        return await transition(story_id)

    world.api.update_run.side_effect = traced_write
    world.api.wait_user_secret_story.side_effect = traced_transition

    await world.enter_the_wait()

    assert trace == [("record", "owed"), ("transition",), ("record", "delivered")]
    record = world.record()
    assert record.state is OwnerNotificationState.DELIVERED
    assert record.delivered_at is not None
    assert (datetime.now(UTC) - record.delivered_at).total_seconds() < 60
    assert _asks_published(world) == 1
    assert "STRIPE_KEY" in world.published[0]["text"]


@pytest.mark.asyncio
async def test_a_lost_publish_starts_the_clock_only_at_its_later_delivery():
    """The ask is owed, the publish fails, and the story waits with no clock.

    However old the owed record grows, nothing expires. The recovery sweep then
    delivers it, and the clock starts at that delivery — not at the owe, and not
    at the consumer's write.
    """
    world = _SecretWait(
        story_status="deploying", consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 4)
    )
    world.publish_fails = True

    await world.enter_the_wait()

    assert world.story_status == "waiting_user_secret"
    assert world.record().state is OwnerNotificationState.OWED
    assert world.record().delivered_at is None
    assert _asks_published(world) == 0
    # The owed record ages far past the bound while delivery is still failing.
    world.set_record(owed_at=_ago(USER_SECRET_BOUND_MINUTES * 3))

    counts, owe, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()

    world.publish_fails = False
    swept_at = datetime.now(UTC)
    await world.sweep_owed_notifications()

    record = world.record()
    assert record.state is OwnerNotificationState.DELIVERED
    assert record.delivered_at >= swept_at
    assert _asks_published(world) == 1
    counts, owe, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unaddressable_owner_is_never_failed_as_unanswered():
    world = _SecretWait(
        story_status="deploying",
        consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 4),
        owner_telegram_id=None,
    )

    await world.enter_the_wait()
    assert world.record().state is OwnerNotificationState.UNADDRESSABLE
    world.set_record(owed_at=_ago(USER_SECRET_BOUND_MINUTES * 3))

    first, owe, _deliver, notify = await _run_watchdog(world.api, world.redis)
    second, owe_again, _deliver_again, notify_again = await _run_watchdog(world.api, world.redis)

    assert first == second == {"parked": 0, "failed": 0}
    assert world.story_status == "waiting_user_secret"
    world.api.fail_story.assert_not_awaited()
    owe.assert_not_awaited()
    owe_again.assert_not_awaited()
    reason = world.quarantine_reason
    assert reason["reason"] == USER_SECRET_REQUEST_UNDELIVERED_REASON
    assert reason["delivery_state"] == "unaddressable"
    assert reason["run_id"] == _SecretWait.RUN_ID
    # Named once, not every tick.
    notify.assert_awaited_once()
    notify_again.assert_not_awaited()
    assert _asks_published(world) == 0


@pytest.mark.asyncio
async def test_an_abandoned_ask_is_never_failed_as_unanswered():
    world = _SecretWait(
        consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 4),
        record=_ask_record(
            OwnerNotificationState.ABANDONED,
            owed_at=_ago(USER_SECRET_BOUND_MINUTES * 3),
            attempts=3,
            detail="ConnectionError: po:input is unavailable",
        ),
    )

    counts, owe, _deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0}
    world.api.fail_story.assert_not_awaited()
    owe.assert_not_awaited()
    assert world.quarantine_reason["reason"] == USER_SECRET_REQUEST_UNDELIVERED_REASON
    assert world.quarantine_reason["delivery_state"] == "abandoned"
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_wait_entered_before_the_ask_was_durable_is_asked_exactly_once():
    """A live story with no ask record is asked now, and never again."""
    world = _SecretWait(consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 5))

    first, owe, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    second, owe_again, _deliver_again, _notify_again = await _run_watchdog(world.api, world.redis)

    assert first == second == {"parked": 0, "failed": 0}
    assert _asks_published(world) == 1
    record = world.record()
    assert record.event is OwnerNotificationEvent.STORY_WAITING_USER_SECRET
    assert record.state is OwnerNotificationState.DELIVERED
    assert (datetime.now(UTC) - record.delivered_at).total_seconds() < 60
    world.api.fail_story.assert_not_awaited()
    owe.assert_not_awaited()
    owe_again.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_crash_between_owe_and_transition_still_asks_once():
    """The retried entry finds the owed record and delivers it; nothing is owed twice."""
    world = _SecretWait(story_status="deploying", consumer_wrote_at=_ago(30))
    await owe_user_secret_request(
        world.api,
        world.run(),
        "story-1",
        "00000000-0000-0000-0000-000000000001",
        logger.bind(story_id="story-1"),
    )
    owed_at = world.record().owed_at

    await world.enter_the_wait()

    record = world.record()
    assert record.owed_at == owed_at
    assert record.state is OwnerNotificationState.DELIVERED
    assert _asks_published(world) == 1


@pytest.mark.asyncio
async def test_a_secret_that_arrives_before_delivery_voids_the_ask():
    """The owner is not asked for a secret the story no longer waits on."""
    world = _SecretWait(story_status="deploying", consumer_wrote_at=_ago(30))
    world.publish_fails = True
    await world.enter_the_wait()
    assert world.record().state is OwnerNotificationState.OWED

    world.story_status = "deploying"  # the secret was saved and the deploy resumed
    world.publish_fails = False
    await world.sweep_owed_notifications()

    assert world.record().state is OwnerNotificationState.VOIDED
    assert _asks_published(world) == 0


# --- no double ending -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_ended_story_is_gone_from_the_scan_on_the_next_tick(api_client, redis_client):
    """The transition is the idempotence: the second tick sees no candidate."""
    story = _make_story(status="deploying")
    scanned: list[list] = [[story], []]

    async def answer(status):
        if status != StoryStatus.DEPLOYING:
            return []
        return scanned.pop(0)

    api_client.get_stories_by_status.side_effect = answer
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.RUNNING, created_at=_ago(DEPLOY_BOUND_MINUTES + 5)
    )

    first, owe, deliver, notify = await _run_watchdog(api_client, redis_client)
    assert first == {"parked": 1, "failed": 0}
    assert owe.await_count == 1

    second, owe_again, deliver_again, notify_again = await _run_watchdog(api_client, redis_client)
    assert second == {"parked": 0, "failed": 0}
    owe_again.assert_not_awaited()
    deliver_again.assert_not_awaited()
    notify_again.assert_not_awaited()
    assert api_client.transition_story.await_count == 1
