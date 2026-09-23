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

from _owner_notification_claims import ClaimClock, claim
from _run_routing_factories import _make_repo, _make_run, _make_story
import pytest
import structlog
from structlog.testing import capture_logs

from shared.contracts.dto.lifecycle_wait import (
    UserSecretWaitCommand,
    UserSecretWaitDisposition,
    UserSecretWaitRead,
)
from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.state_wait import (
    STATE_AGE_BOUND_REASON,
    StateWaitExpiryCommand,
    StateWaitExpiryDisposition,
    StateWaitExpiryRead,
    StateWaitObservation,
    StateWaitSkipReason,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from src.tasks.owner_notifications import (
    read_owner_notification,
    supervise_owed_owner_notifications,
)
from src.tasks.supervisor.deploy import (
    _handle_deploy_waiting_user_secret,
    owe_user_secret_request,
)
from src.tasks.supervisor.state_age import (
    STATE_AGE_BOUNDS,
    USER_SECRET_REQUEST_UNDELIVERED_REASON,
    supervise_state_age_bounds,
)

DEPLOY_BOUND_MINUTES = 30
QA_BOUND_MINUTES = 60
PR_REVIEW_BOUND_MINUTES = 220
USER_SECRET_BOUND_MINUTES = 1440

logger = structlog.get_logger(__name__)

_UNCHANGED = object()


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


def _observation(status, run, *, pr_number=None, secrets_saved=False) -> StateWaitObservation:
    """What the API's locked rows would show for this story and its latest Run."""
    return StateWaitObservation(
        status=status,
        pr_number=pr_number,
        run_id=None if run is None else run.id,
        run_status=None if run is None else run.status,
        ask=None if run is None else read_owner_notification(run),
        secrets_saved=secrets_saved,
    )


def _decide(story_id: str, command: StateWaitExpiryCommand, seen, stored_reason=None):
    """The API's expire-state-wait decision, made by the contract's own comparison."""
    if command.is_repeat(seen.status.value, stored_reason):
        return StateWaitExpiryRead(
            disposition=StateWaitExpiryDisposition.ALREADY_ENDED,
            story_id=story_id,
            story_status=seen.status,
        )
    skip = command.mismatch(seen)
    if skip is not None:
        return StateWaitExpiryRead(
            disposition=StateWaitExpiryDisposition.SKIPPED,
            story_id=story_id,
            story_status=seen.status,
            skip=skip,
        )
    return StateWaitExpiryRead(
        disposition=StateWaitExpiryDisposition.EXPIRED,
        story_id=story_id,
        story_status=command.terminal_status,
    )


class _Guard:
    """The API's guarded ending, against the world as the test left it.

    Unless a test moves something, the story is exactly where the watchdog saw
    it: its status is the expected one, its latest Run is the one the watchdog
    read, and its pull request number is unchanged. A test that moves the story
    on sets what the locked rows would show instead. An ending that commits is
    remembered, so a repeat of it is answered as the API answers one.
    """

    def __init__(self, client) -> None:
        self.client = client
        self.status = _UNCHANGED
        self.run = _UNCHANGED
        self.pr_number = _UNCHANGED
        self.secrets_saved = False
        self.ended: dict[str, tuple[StoryStatus, dict]] = {}

    async def expire(self, story_id: str, command: StateWaitExpiryCommand):
        if story_id in self.ended:
            status, reason = self.ended[story_id]
            seen = StateWaitObservation(status=status)
            return _decide(story_id, command, seen, reason)
        run = self.run
        if run is _UNCHANGED:
            # A pull-request wait has no anchor Run; the others read the one the
            # watchdog read.
            anchored = command.anchor.run_id is not None
            run = self.client.get_latest_run_by_story.return_value if anchored else None
        seen = _observation(
            command.expected_status if self.status is _UNCHANGED else self.status,
            run,
            pr_number=command.anchor.pr_number if self.pr_number is _UNCHANGED else self.pr_number,
            secrets_saved=self.secrets_saved,
        )
        ended = _decide(story_id, command, seen)
        if ended.disposition is StateWaitExpiryDisposition.EXPIRED:
            self.ended[story_id] = (command.terminal_status, command.reason.model_dump(mode="json"))
        return ended


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_stories_by_status.side_effect = _stories_by_status("none", [])
    client.guard = _Guard(client)
    client.expire_state_wait.side_effect = client.guard.expire
    return client


@pytest.fixture
def redis_client():
    return AsyncMock()


def _patches():
    return (
        patch("src.tasks.supervisor.state_age.notify_admins_best_effort", new_callable=AsyncMock),
        patch("src.tasks.supervisor.state_age.deliver_owed_notification", new_callable=AsyncMock),
        patch("src.tasks.supervisor.state_age.GitHubAppClient"),
    )


async def _run_watchdog(api_client, redis_client, *, pull_request: dict | list | None = None):
    """Run the sweep with its delivery, administrator alert and GitHub reads doubled.

    Returns the counts, the guarded ending the sweep asked the API for, and the
    delivery and alert doubles. ``pull_request`` may be a list: one GitHub read
    per item, in order, which is how a pull request that moves between the
    watchdog's two reads is written.
    """
    notify_p, deliver_p, github_p = _patches()
    with notify_p as notify, deliver_p as deliver, github_p as github_cls:
        github = AsyncMock()
        github_cls.return_value = github
        if isinstance(pull_request, list):
            github.get_pull_request.side_effect = pull_request
        elif pull_request is not None:
            github.get_pull_request.return_value = pull_request
        counts = await supervise_state_age_bounds(api_client, redis_client)
        return counts, api_client.expire_state_wait, deliver, notify


def _ended_command(ended) -> StateWaitExpiryCommand:
    ended.assert_awaited_once()
    story_id, command = ended.await_args.args
    assert story_id == "story-1"
    return command


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


def _assert_parked_once(api_client, ended, deliver, notify, *, status: str, anchor: str):
    """One guarded ending carrying reason, owed record and park; then delivery and alert."""
    command = _ended_command(ended)
    assert command.expected_status == status
    reason = command.reason.model_dump(mode="json")
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == status
    assert reason["anchor"] == anchor
    assert reason["ending"] == "park"
    assert reason["threshold_minutes"] > 0
    record = command.owner_notification
    assert record.event is OwnerNotificationEvent.STORY_BLOCKED
    assert record.terminal_status is StoryStatus.WAITING_HUMAN_REVIEW
    assert record.state is OwnerNotificationState.OWED
    # Nothing is written outside the guarded action.
    api_client.update_story.assert_not_awaited()
    api_client.update_story_owner_notification.assert_not_awaited()
    api_client.transition_story.assert_not_awaited()
    api_client.fail_story.assert_not_awaited()
    deliver.assert_awaited_once()
    assert deliver.await_args.args[2] == "story-1"
    assert deliver.await_args.args[3] == record
    assert deliver.await_args.kwargs["story_record"] is True
    notify.assert_awaited_once()


def _assert_skipped(ended, deliver, notify, logs, *, mismatch: StateWaitSkipReason) -> None:
    """Nothing reached anybody, and the skip is one structured line naming why."""
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    skipped = [entry for entry in logs if entry["event"] == "state_age_bound_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["story_id"] == "story-1"
    assert skipped[0]["mismatch"] == mismatch.value
    assert {"expected_status", "actual_status", "expected", "actual"} <= skipped[0].keys()
    assert not [entry for entry in logs if entry["event"] == "state_age_bound_expired"]


# --- deploying ------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploying_with_an_old_running_deploy_run_is_parked_once(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.RUNNING, created_at=_ago(DEPLOY_BOUND_MINUTES + 5)
    )

    counts, ended, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 1, "failed": 0, "skipped": 0}
    _assert_parked_once(
        api_client,
        ended,
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

    counts, ended, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()
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

    counts, ended, _deliver, _notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_redispatched_deploy_restarts_the_bound(api_client, redis_client):
    """Progress is a new Run, and the bound is measured from that Run."""
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying", created_at=_ago(600))]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        id="deploy-2", status=RunStatus.QUEUED, created_at=_ago(1)
    )

    counts, ended, _deliver, _notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()


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

    counts, ended, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 1, "failed": 0, "skipped": 0}
    _assert_parked_once(
        api_client, ended, deliver, notify, status="testing", anchor="qa_run_created_at"
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

    counts, ended, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()
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

    counts, ended, deliver, notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES + 30)),
    )

    assert counts == {"parked": 1, "failed": 0, "skipped": 0}
    _assert_parked_once(
        api_client,
        ended,
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

    counts, ended, deliver, notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES - 10)),
    )

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()
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

    counts, ended, _deliver, _notify = await _run_watchdog(
        api_client,
        redis_client,
        pull_request=_pull_request(
            updated_at=merged,
            state="closed",
            merged_at=merged.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_pull_request_never_ends_a_wait(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()

    notify_p, deliver_p, github_p = _patches()
    with notify_p, deliver_p, github_p as github_cls:
        github = AsyncMock()
        github_cls.return_value = github
        github.get_pull_request.side_effect = RuntimeError("GitHub is unreachable")
        counts = await supervise_state_age_bounds(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    api_client.expire_state_wait.assert_not_awaited()


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
        self.story_record: OwnerNotification | None = None
        self.secrets_saved = False
        self.published: list[dict] = []
        self.publish_fails = False

        api = AsyncMock()
        api.get_stories_by_status.side_effect = self._stories
        api.get_latest_run_by_story.side_effect = self._latest_run
        api.update_run.side_effect = self._update_run
        api.get_story.side_effect = self._story
        api.update_story.side_effect = self._update_story
        api.fail_story.side_effect = self._fail_story
        api.expire_state_wait.side_effect = self._expire_state_wait
        api.park_waiting_user_secret.side_effect = self._park_waiting_user_secret
        api.list_runs_owing_owner_notification.side_effect = self._runs_owing
        api.list_stories_owing_owner_notification.return_value = []
        api.get_project.return_value = SimpleNamespace(owner_id=555)
        api.get_user.return_value = SimpleNamespace(telegram_id=owner_telegram_id)
        api.claim_run_owner_notification_attempt.side_effect = self._claim_run
        self.clock = ClaimClock()
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

    async def _claim_run(self, run_id):
        assert run_id == self.RUN_ID
        return claim(
            self.clock,
            lambda: self.run_metadata.get(OWNER_NOTIFICATION_KEY),
            lambda stamped: self.run_metadata.update({OWNER_NOTIFICATION_KEY: stamped}),
        )

    async def _story(self, story_id):
        return self._story_dto()

    async def _update_story(self, story_id, data):
        self.quarantine_reason = data.get("quarantine_reason", self.quarantine_reason)
        return self._story_dto()

    async def _fail_story(self, story_id):
        self.story_status = "failed"
        return self._story_dto()

    async def _expire_state_wait(self, story_id, command):
        seen = _observation(
            StoryStatus(self.story_status), self.run(), secrets_saved=self.secrets_saved
        )
        ended = _decide(story_id, command, seen, self.quarantine_reason)
        if ended.disposition is StateWaitExpiryDisposition.EXPIRED:
            self.quarantine_reason = command.reason.model_dump(mode="json")
            self.story_record = command.owner_notification
            self.story_status = command.terminal_status.value
        return ended

    async def _park_waiting_user_secret(self, story_id, command: UserSecretWaitCommand):
        """The API action: the transition and the Run's ask in one transaction.

        An ask the Run already carries is kept; a story already waiting is a
        repeat that writes nothing.
        """
        assert command.run_id == self.RUN_ID
        existing = self.record()
        if self.story_status == "waiting_user_secret":
            return UserSecretWaitRead(
                disposition=UserSecretWaitDisposition.ALREADY_WAITING,
                story_id=story_id,
                story_status=StoryStatus.WAITING_USER_SECRET,
                run_id=command.run_id,
                owner_notification=existing,
            )
        assert self.story_status == "deploying"
        ask = existing
        if existing is None or existing.state is OwnerNotificationState.VOIDED:
            ask = OwnerNotification(
                event=OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
                text=command.text,
                story_id=story_id,
                project_id="00000000-0000-0000-0000-000000000001",
                terminal_status=StoryStatus.WAITING_USER_SECRET,
                state=OwnerNotificationState.OWED,
                owed_at=datetime.now(UTC),
            )
            self.run_metadata = {
                **self.run_metadata,
                OWNER_NOTIFICATION_KEY: ask.model_dump(mode="json"),
            }
        self.story_status = "waiting_user_secret"
        return UserSecretWaitRead(
            disposition=UserSecretWaitDisposition.WAITING,
            story_id=story_id,
            story_status=StoryStatus.WAITING_USER_SECRET,
            run_id=command.run_id,
            owner_notification=ask,
        )

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
        """A later cycle's sweep: at least one delivery interval after the last attempt."""
        self.clock.elapse()
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

    counts, ended, _deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    assert world.story_status == "waiting_user_secret"
    ended.assert_not_awaited()
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

    counts, ended, deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 1, "skipped": 0}
    reason = world.quarantine_reason
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == "waiting_user_secret"
    assert reason["anchor"] == "user_secret_request_delivered_at"
    assert reason["ending"] == "fail"
    command = _ended_command(ended)
    assert command.anchor.run_id == _SecretWait.RUN_ID
    assert command.anchor.ask_delivered_at == world.record().delivered_at
    assert world.story_status == "failed"
    assert world.story_record.event is OwnerNotificationEvent.STORY_FAILED
    assert world.story_record.terminal_status is StoryStatus.FAILED
    world.api.fail_story.assert_not_awaited()
    world.api.update_story.assert_not_awaited()
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

    counts, ended, deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    world.api.fail_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_entering_the_wait_owes_the_ask_with_the_transition_and_delivers_it():
    """The ask is owed in the transition's own API action, then delivered through the seam.

    Replaces the three-call order (owe on the Run, transition, deliver): the ask
    and the wait now commit in one transaction, so no ask is owed for a wait that
    did not start and no wait starts without its ask.
    """
    world = _SecretWait(story_status="deploying", consumer_wrote_at=_ago(30))
    trace: list[tuple[str, ...]] = []
    write_run, park = world._update_run, world._park_waiting_user_secret

    async def traced_write(run_id, data):
        trace.append(("record", data["run_metadata"][OWNER_NOTIFICATION_KEY]["state"]))
        await write_run(run_id, data)

    async def traced_park(story_id, command):
        answer = await park(story_id, command)
        trace.append(("park", world.story_status, world.record().state.value))
        return answer

    world.api.update_run.side_effect = traced_write
    world.api.park_waiting_user_secret.side_effect = traced_park

    await world.enter_the_wait()

    assert trace == [("park", "waiting_user_secret", "owed"), ("record", "delivered")]
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

    counts, ended, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()

    world.publish_fails = False
    swept_at = datetime.now(UTC)
    await world.sweep_owed_notifications()

    record = world.record()
    assert record.state is OwnerNotificationState.DELIVERED
    assert record.delivered_at >= swept_at
    assert _asks_published(world) == 1
    counts, ended, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    ended.assert_not_awaited()


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

    first, ended, _deliver, notify = await _run_watchdog(world.api, world.redis)
    second, _ended, _deliver_again, notify_again = await _run_watchdog(world.api, world.redis)

    assert first == second == {"parked": 0, "failed": 0, "skipped": 0}
    assert world.story_status == "waiting_user_secret"
    world.api.fail_story.assert_not_awaited()
    ended.assert_not_awaited()
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

    counts, ended, _deliver, notify = await _run_watchdog(world.api, world.redis)

    assert counts == {"parked": 0, "failed": 0, "skipped": 0}
    world.api.fail_story.assert_not_awaited()
    ended.assert_not_awaited()
    assert world.quarantine_reason["reason"] == USER_SECRET_REQUEST_UNDELIVERED_REASON
    assert world.quarantine_reason["delivery_state"] == "abandoned"
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_wait_entered_before_the_ask_was_durable_is_asked_exactly_once():
    """A live story with no ask record is asked now, and never again."""
    world = _SecretWait(consumer_wrote_at=_ago(USER_SECRET_BOUND_MINUTES * 5))

    first, ended, _deliver, _notify = await _run_watchdog(world.api, world.redis)
    second, _ended, _deliver_again, _notify_again = await _run_watchdog(world.api, world.redis)

    assert first == second == {"parked": 0, "failed": 0, "skipped": 0}
    assert _asks_published(world) == 1
    record = world.record()
    assert record.event is OwnerNotificationEvent.STORY_WAITING_USER_SECRET
    assert record.state is OwnerNotificationState.DELIVERED
    assert (datetime.now(UTC) - record.delivered_at).total_seconds() < 60
    world.api.fail_story.assert_not_awaited()
    ended.assert_not_awaited()


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
    """The committed transition is the idempotence: the second tick sees no candidate."""
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

    first, ended, deliver, notify = await _run_watchdog(api_client, redis_client)
    assert first == {"parked": 1, "failed": 0, "skipped": 0}
    assert ended.await_count == 1

    second, _ended, deliver_again, notify_again = await _run_watchdog(api_client, redis_client)
    assert second == {"parked": 0, "failed": 0, "skipped": 0}
    assert ended.await_count == 1
    deliver_again.assert_not_awaited()
    notify_again.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_repeated_ending_of_an_ended_wait_is_a_typed_no_op(api_client, redis_client):
    """A pass that still read the old status asks again; the API answers already_ended.

    The first ending committed; the second pass read the story before it did.
    The repeat writes nothing, is not counted, and tells nobody a second time.
    """
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.DEPLOYING, [_make_story(status="deploying")]
    )
    api_client.get_latest_run_by_story.return_value = _make_run(
        status=RunStatus.RUNNING, created_at=_ago(DEPLOY_BOUND_MINUTES + 5)
    )

    first, ended, deliver, notify = await _run_watchdog(api_client, redis_client)
    with capture_logs() as logs:
        second, _ended, deliver_again, notify_again = await _run_watchdog(api_client, redis_client)

    assert first == {"parked": 1, "failed": 0, "skipped": 0}
    assert second == {"parked": 0, "failed": 0, "skipped": 0}
    assert ended.await_count == 2
    assert api_client.guard.ended["story-1"][0] is StoryStatus.WAITING_HUMAN_REVIEW
    deliver.assert_awaited_once()
    notify.assert_awaited_once()
    deliver_again.assert_not_awaited()
    notify_again.assert_not_awaited()
    assert [entry["event"] for entry in logs if entry["event"].startswith("state_age")] == [
        "state_age_bound_already_ended"
    ]


# --- the story moved on between the read and the ending -------------------
#
# Each test lets the watchdog read an expired wait, then has the API's locked
# rows show something else — what routing leaves behind when it moves the story
# on in the same pass. The decision is the contract's own comparison
# (`StateWaitExpiryCommand.mismatch`), which is what the API runs. Every skip
# writes nothing, delivers nothing, alerts nobody, and is one structured line.


def _expired_deploy_wait(api_client, *, run_type=RunType.DEPLOY, status="deploying"):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus(status), [_make_story(status=status)]
    )
    run = _make_run(
        id=f"{run_type.value}-1",
        type=run_type,
        status=RunStatus.RUNNING,
        created_at=_ago(QA_BOUND_MINUTES * 2),
    )
    api_client.get_latest_run_by_story.return_value = run
    return run


async def _watch_skip(api_client, redis_client, *, pull_request=None):
    with capture_logs() as logs:
        counts, ended, deliver, notify = await _run_watchdog(
            api_client, redis_client, pull_request=pull_request
        )
    return counts, ended, deliver, notify, logs


@pytest.mark.asyncio
async def test_a_story_routing_moved_to_another_status_is_skipped(api_client, redis_client):
    """deploying → testing by the deploy supervisor, after the watchdog read deploying."""
    _expired_deploy_wait(api_client)
    api_client.guard.status = StoryStatus.TESTING

    counts, ended, deliver, notify, logs = await _watch_skip(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    ended.assert_awaited_once()
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.STATUS_MOVED)
    skipped = next(entry for entry in logs if entry["event"] == "state_age_bound_skipped")
    assert skipped["expected_status"] == "deploying"
    assert skipped["actual_status"] == "testing"
    assert api_client.guard.ended == {}


@pytest.mark.asyncio
async def test_a_wait_whose_run_was_replaced_by_a_redispatch_is_skipped(api_client, redis_client):
    """A re-dispatch made a new Run: the bound restarts from it, so the old one ends nothing."""
    observed = _expired_deploy_wait(api_client)
    api_client.guard.run = _make_run(id="deploy-2", status=RunStatus.QUEUED, created_at=_ago(0))

    counts, ended, deliver, notify, logs = await _watch_skip(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    assert _ended_command(ended).anchor.run_id == observed.id
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.RUN_REPLACED)
    skipped = next(entry for entry in logs if entry["event"] == "state_age_bound_skipped")
    assert (skipped["expected"], skipped["actual"]) == (observed.id, "deploy-2")


@pytest.mark.asyncio
async def test_a_wait_whose_run_reported_since_is_skipped(api_client, redis_client):
    """The QA run finished after the read: its outcome is routing's, not a timeout."""
    observed = _expired_deploy_wait(api_client, run_type=RunType.QA, status="testing")
    api_client.guard.run = observed.model_copy(update={"status": RunStatus.COMPLETED})

    counts, ended, deliver, notify, logs = await _watch_skip(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.RUN_TERMINAL)


def _delivered_secret_wait(api_client, *, delivered_at: datetime):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.WAITING_USER_SECRET, [_make_story(status="waiting_user_secret")]
    )
    run = _make_run(
        id=_SecretWait.RUN_ID,
        status=RunStatus.COMPLETED,
        run_metadata={
            OWNER_NOTIFICATION_KEY: _ask_record(
                OwnerNotificationState.DELIVERED,
                owed_at=delivered_at - timedelta(minutes=1),
                delivered_at=delivered_at,
                attempts=1,
            )
        },
        result={
            "deploy_outcome": "waiting_for_user_secret",
            "missing_user_secrets": [{"key": "STRIPE_KEY", "description": "Stripe secret key"}],
        },
    )
    api_client.get_latest_run_by_story.return_value = run
    return run


@pytest.mark.asyncio
async def test_a_secret_wait_whose_ask_record_changed_is_skipped(api_client, redis_client):
    """The ask on the locked Run is no longer delivered at the moment the age was taken."""
    observed = _delivered_secret_wait(api_client, delivered_at=_ago(USER_SECRET_BOUND_MINUTES * 2))
    asked_again = _ask_record(
        OwnerNotificationState.DELIVERED,
        owed_at=_ago(2),
        delivered_at=_ago(1),
        attempts=1,
    )
    api_client.guard.run = observed.model_copy(
        update={"run_metadata": {OWNER_NOTIFICATION_KEY: asked_again}}
    )

    counts, ended, deliver, notify, logs = await _watch_skip(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    command = _ended_command(ended)
    assert command.ending.value == "fail"
    assert command.anchor.ask_delivered_at == read_owner_notification(observed).delivered_at
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.ASK_RECORD_CHANGED)


@pytest.mark.asyncio
async def test_a_secret_wait_whose_secrets_were_saved_is_not_failed(api_client, redis_client):
    """The subject of the wait arrived: routing resumes the deploy, the owner is not failed."""
    _delivered_secret_wait(api_client, delivered_at=_ago(USER_SECRET_BOUND_MINUTES * 2))
    api_client.guard.secrets_saved = True

    counts, ended, deliver, notify, logs = await _watch_skip(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.SECRETS_SAVED)


@pytest.mark.asyncio
async def test_a_pull_request_that_moved_after_the_read_is_skipped_without_asking(
    api_client, redis_client
):
    """GitHub is not a locked row: the PR is read again right before the ending."""
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()
    stale = _pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES + 30))
    merged = _pull_request(
        updated_at=_ago(0), state="closed", merged_at=_ago(0).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    counts, ended, deliver, notify, logs = await _watch_skip(
        api_client, redis_client, pull_request=[stale, merged]
    )

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    ended.assert_not_awaited()
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.PR_UPDATED_AT_MOVED)


@pytest.mark.asyncio
async def test_a_pull_request_unreadable_on_the_second_read_is_skipped(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()
    stale = _pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES + 30))

    counts, ended, deliver, notify, logs = await _watch_skip(
        api_client, redis_client, pull_request=[stale, RuntimeError("GitHub is unreachable")]
    )

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    ended.assert_not_awaited()
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.PR_UPDATED_AT_MOVED)


@pytest.mark.asyncio
async def test_a_story_that_now_names_another_pull_request_is_skipped(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.PR_REVIEW, [_make_story(status="pr_review", pr_number=42)]
    )
    api_client.get_primary_repository.return_value = _make_repo()
    api_client.guard.pr_number = 43

    counts, ended, deliver, notify, logs = await _watch_skip(
        api_client,
        redis_client,
        pull_request=_pull_request(updated_at=_ago(PR_REVIEW_BOUND_MINUTES + 30)),
    )

    assert counts == {"parked": 0, "failed": 0, "skipped": 1}
    assert _ended_command(ended).anchor.pr_number == 42
    _assert_skipped(ended, deliver, notify, logs, mismatch=StateWaitSkipReason.PR_NUMBER_CHANGED)
