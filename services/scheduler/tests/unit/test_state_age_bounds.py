"""Every bounded wait ends, once, with a typed reason and a told owner.

One test per bounded state with an aged record, and one per state just under the
bound that must be left alone. The four states are deliberately exercised
through the same public watchdog: there is one map and one sweep, so a state
that needed its own entry point would be visible here as a second call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_repo, _make_run, _make_story
import pytest

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from src.tasks.supervisor.state_age import (
    STATE_AGE_BOUND_REASON,
    STATE_AGE_BOUNDS,
    supervise_state_age_bounds,
)

DEPLOY_BOUND_MINUTES = 30
QA_BOUND_MINUTES = 60
PR_REVIEW_BOUND_MINUTES = 240
USER_SECRET_BOUND_MINUTES = 1440


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


def _waiting_secret_run(*, asked_at: datetime):
    return _make_run(
        id="deploy-secret-source",
        status=RunStatus.COMPLETED,
        created_at=asked_at - timedelta(minutes=5),
        updated_at=asked_at,
        result={
            "deploy_outcome": "waiting_for_user_secret",
            "missing_user_secrets": [{"key": "STRIPE_KEY", "description": "Stripe secret key"}],
        },
    )


@pytest.mark.asyncio
async def test_an_unanswered_secret_request_fails_the_story_once(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.WAITING_USER_SECRET, [_make_story(status="waiting_user_secret")]
    )
    api_client.get_latest_run_by_story.return_value = _waiting_secret_run(
        asked_at=_ago(USER_SECRET_BOUND_MINUTES + 60)
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 1}
    reason = api_client.update_story.await_args.args[1]["quarantine_reason"]
    assert reason["reason"] == STATE_AGE_BOUND_REASON
    assert reason["status"] == "waiting_user_secret"
    assert reason["anchor"] == "user_secret_requested_at"
    assert reason["ending"] == "fail"
    owe.assert_awaited_once()
    assert owe.await_args.kwargs["event"] is OwnerNotificationEvent.STORY_FAILED
    assert owe.await_args.kwargs["terminal_status"] is StoryStatus.FAILED
    api_client.fail_story.assert_awaited_once_with("story-1")
    api_client.transition_story.assert_not_awaited()
    deliver.assert_awaited_once()
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_secret_request_just_under_the_bound_is_left_alone(api_client, redis_client):
    api_client.get_stories_by_status.side_effect = _stories_by_status(
        StoryStatus.WAITING_USER_SECRET, [_make_story(status="waiting_user_secret")]
    )
    api_client.get_latest_run_by_story.return_value = _waiting_secret_run(
        asked_at=_ago(USER_SECRET_BOUND_MINUTES - 60)
    )

    counts, owe, deliver, notify = await _run_watchdog(api_client, redis_client)

    assert counts == {"parked": 0, "failed": 0}
    owe.assert_not_awaited()
    deliver.assert_not_awaited()
    notify.assert_not_awaited()
    api_client.fail_story.assert_not_awaited()


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
