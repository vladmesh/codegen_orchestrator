"""One age bound per waiting Story state, and the single watchdog that applies them.

Four Story states used to wait without an ending: `deploying` with a run that
never reports, `testing` with a QA run that never reports, `pr_review` with a
pull request that never merges, and `waiting_user_secret` with a user who never
answers. Each of them is a place where a user's work can stop moving and nobody
is told, which is the failure this module removes.

It is one map and one watchdog rather than four timeout branches. The map says,
per Story status, how long the wait may last, where its age is measured from,
and how the wait ends; the watchdog applies all of them the same way. A new
bounded state is one entry plus its config key.

What a bound is measured from is the part that has to be defensible, so each
entry names its own anchor instead of sharing the Story row's ``updated_at``,
which any unrelated write moves:

* ``deploying`` / ``testing`` — the in-flight Run's own ``created_at``. Only a
  QUEUED or RUNNING run is a wait at all; a terminal run is routed by the deploy
  and QA supervisors on the same tick. A re-dispatch is a *new* Run, so the
  bound restarts exactly when the platform genuinely started over, and nothing
  else can reset it.
* ``waiting_user_secret`` — the deploy Run that reported the missing secrets,
  measured from the moment its result was written. The request to the user is
  emitted on that same tick, by `_handle_deploy_waiting_user_secret`, and the
  resume path creates a new Run rather than touching this one, so the run's last
  write is the moment the user was asked and nothing moves it afterwards.
* ``pr_review`` — the pull request's own ``updated_at`` on GitHub. It lives
  outside this process, so it survives a tick restart; it moves when the pull
  request moves (a push, an update-branch, the merge) and not when something
  unrelated writes the story row. A pull request nothing is doing anything to is
  precisely one whose ``updated_at`` stands still.

Its relation to ``IMAGE_PUBLICATION_TIMEOUT_SECONDS`` (900 s), the other bound
spent inside ``pr_review``: that one is measured from the merge and always ends,
in a refusal that parks the story itself. This bound is an order of magnitude
longer, so a merged story still inside the image window — which already has an
ending — is never taken by this watchdog; it only catches a wait nothing else
bounds at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import ValidationError
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import notify_admins_best_effort
from shared.redis import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from .._github_refs import _parse_github_timestamp, _parse_owner_repo
from ..owner_notifications import deliver_owed_notification, owe_story_owner_notification
from .common import STORY_HUMAN_REVIEW_ACTION, _parse_datetime

logger = structlog.get_logger(__name__)

#: The typed reason a story carries when one of these bounds ended its wait.
STATE_AGE_BOUND_REASON = "state_wait_age_bound_exceeded"

#: A Run in one of these statuses is still a wait. A terminal Run is an outcome
#: the deploy/QA supervisors route on the same tick, never something to bound.
_IN_FLIGHT_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


class WaitEnding(StrEnum):
    """How an expired wait ends.

    ``PARK`` hands the story to the human-review queue: the platform could not
    finish, which is not evidence that the product is broken. ``FAIL`` is for a
    wait whose subject is outside the platform and simply never came — the only
    honest ending, and the only one ``waiting_user_secret`` even has a
    transition for.
    """

    PARK = "park"
    FAIL = "fail"


_TERMINAL_STATUS_BY_ENDING: dict[WaitEnding, StoryStatus] = {
    WaitEnding.PARK: StoryStatus.WAITING_HUMAN_REVIEW,
    WaitEnding.FAIL: StoryStatus.FAILED,
}

_OWNER_EVENT_BY_ENDING: dict[WaitEnding, OwnerNotificationEvent] = {
    WaitEnding.PARK: OwnerNotificationEvent.STORY_BLOCKED,
    WaitEnding.FAIL: OwnerNotificationEvent.STORY_FAILED,
}

AnchorResolver = Callable[
    ["SchedulerAPIClient", StoryDTO, GitHubAppClient],
    Awaitable[datetime | None],
]


@dataclass(frozen=True)
class StateAgeBound:
    """One waiting state's bound: how long, measured from where, ending how."""

    status: StoryStatus
    config_key: str
    #: What the age is measured from, in the words the report and the typed
    #: reason use. Not a free-text log string: it is part of the evidence.
    anchor: str
    ending: WaitEnding
    resolve_anchor: AnchorResolver
    owner_text: Callable[[int], str]


def _threshold_minutes(bound: StateAgeBound) -> int:
    return startup.get_config().get_int(bound.config_key)


def _age_minutes(moment: datetime) -> float:
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - reference).total_seconds() / 60


async def _in_flight_run_anchor(
    api_client: SchedulerAPIClient,
    story: StoryDTO,
    run_type: RunType,
) -> datetime | None:
    """When the Run this story is waiting on started, or None if it is not waiting."""
    log = logger.bind(story_id=story.id, run_type=run_type.value)
    try:
        run = await api_client.get_latest_run_by_story(story.id, run_type=run_type.value)
    except ValidationError:
        # The routing supervisor reads the same run on this tick and fails the
        # story on it loudly. Nothing to bound and nothing to hide.
        log.info("state_age_bound_unreadable_run")
        return None
    if run is None or run.status not in _IN_FLIGHT_RUN_STATUSES:
        return None
    return _parse_datetime(run.created_at)


async def _deploy_run_anchor(
    api_client: SchedulerAPIClient, story: StoryDTO, _github: GitHubAppClient
) -> datetime | None:
    return await _in_flight_run_anchor(api_client, story, RunType.DEPLOY)


async def _qa_run_anchor(
    api_client: SchedulerAPIClient, story: StoryDTO, _github: GitHubAppClient
) -> datetime | None:
    return await _in_flight_run_anchor(api_client, story, RunType.QA)


async def _user_secret_request_anchor(
    api_client: SchedulerAPIClient, story: StoryDTO, _github: GitHubAppClient
) -> datetime | None:
    """When the user was asked for the secrets this story is still missing."""
    log = logger.bind(story_id=story.id)
    try:
        run = await api_client.get_latest_run_by_story(story.id, run_type=RunType.DEPLOY.value)
    except ValidationError:
        log.info("state_age_bound_unreadable_run", run_type=RunType.DEPLOY.value)
        return None
    if run is None or run.result is None or not run.result.missing_user_secrets:
        return None
    # The ask is published on the tick that wrote this result, and nothing
    # writes to this run again while the story waits, so the run's last write is
    # the moment of the ask. A run that has never been written since creation
    # cannot be carrying a result, so there is nothing to invent here.
    return _parse_datetime(run.updated_at) if run.updated_at else None


async def _pull_request_anchor(
    api_client: SchedulerAPIClient, story: StoryDTO, github: GitHubAppClient
) -> datetime | None:
    """When this story's pull request last moved, as GitHub records it."""
    log = logger.bind(story_id=story.id)
    if not story.pr_number:
        return None
    repo = await api_client.get_primary_repository(str(story.project_id))
    if not repo:
        return None
    owner, repo_name = _parse_owner_repo(repo.git_url or "")
    try:
        pull_request = await github.get_pull_request(owner, repo_name, story.pr_number)
    except Exception:
        # An unreadable pull request is not evidence of a stuck story, and this
        # watchdog never ends a wait on an observation it could not make.
        log.warning("state_age_bound_pull_request_unreadable", pr_number=story.pr_number)
        return None
    return _parse_github_timestamp(pull_request.get("updated_at")) or _parse_github_timestamp(
        pull_request.get("created_at")
    )


def _deploy_owner_text(threshold_minutes: int) -> str:
    return (
        "The deployment of this change has not reported anything for over "
        f"{threshold_minutes} minutes, so it is not going to finish on its own. "
        "A specialist has to look at this; nothing more happens automatically."
    )


def _qa_owner_text(threshold_minutes: int) -> str:
    return (
        "The automatic testing of this change has not reported anything for over "
        f"{threshold_minutes} minutes, so it is not going to finish on its own. "
        "A specialist has to look at this; nothing more happens automatically."
    )


def _pr_review_owner_text(threshold_minutes: int) -> str:
    return (
        "The finished pull request for this change has not moved for over "
        f"{threshold_minutes} minutes and was never merged, so nothing is being "
        "deployed. A specialist has to look at this; nothing more happens "
        "automatically."
    )


def _user_secret_owner_text(threshold_minutes: int) -> str:
    return (
        "The deployment has been waiting over "
        f"{threshold_minutes} minutes for secrets only you can provide, and they "
        "have not arrived, so this change has been stopped. Save the values and "
        "ask for the change again to start it over."
    )


#: The map. One entry per bounded state; the watchdog below is the only thing
#: that reads it, and adding a state means adding a line here and its config key
#: to `scripts/system_configs.yaml`, `startup.PIPELINE_REQUIRED_KEYS` and the
#: scheduler test conftest.
#:
#: Keyed by Story status because the status is what the scan reads. The
#: `waiting_on` each status implies is already declared once, in
#: `WAITING_ON_BY_STATUS`, and rides along in the typed reason instead of being
#: restated here.
STATE_AGE_BOUNDS: tuple[StateAgeBound, ...] = (
    StateAgeBound(
        status=StoryStatus.DEPLOYING,
        config_key="supervisor.deploy_wait_max_minutes",
        anchor="deploy_run_created_at",
        ending=WaitEnding.PARK,
        resolve_anchor=_deploy_run_anchor,
        owner_text=_deploy_owner_text,
    ),
    StateAgeBound(
        status=StoryStatus.TESTING,
        config_key="supervisor.qa_wait_max_minutes",
        anchor="qa_run_created_at",
        ending=WaitEnding.PARK,
        resolve_anchor=_qa_run_anchor,
        owner_text=_qa_owner_text,
    ),
    StateAgeBound(
        status=StoryStatus.PR_REVIEW,
        config_key="supervisor.pr_review_wait_max_minutes",
        anchor="github_pull_request_updated_at",
        ending=WaitEnding.PARK,
        resolve_anchor=_pull_request_anchor,
        owner_text=_pr_review_owner_text,
    ),
    StateAgeBound(
        status=StoryStatus.WAITING_USER_SECRET,
        config_key="supervisor.user_secret_wait_max_minutes",
        anchor="user_secret_requested_at",
        ending=WaitEnding.FAIL,
        resolve_anchor=_user_secret_request_anchor,
        owner_text=_user_secret_owner_text,
    ),
)


async def supervise_state_age_bounds(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Apply every state age bound once, and end the waits that are over.

    Returns the number of stories parked and failed by the bounds this tick.
    """
    counts = {"parked": 0, "failed": 0}
    github = GitHubAppClient()

    for bound in STATE_AGE_BOUNDS:
        threshold = _threshold_minutes(bound)
        for story in await api_client.get_stories_by_status(bound.status):
            log = logger.bind(
                story_id=story.id,
                project_id=str(story.project_id),
                status=bound.status.value,
            )
            anchor = await bound.resolve_anchor(api_client, story, github)
            if anchor is None:
                continue
            age = _age_minutes(anchor)
            if age < threshold:
                continue
            # One story's ending must not stop the others: they are unrelated
            # work, and a sweep that dies on the first broken story leaves every
            # later one waiting exactly as long as the failure lasts.
            try:
                await _end_expired_wait(
                    api_client,
                    redis_client,
                    story=story,
                    bound=bound,
                    anchor=anchor,
                    age_minutes=age,
                    threshold_minutes=threshold,
                    log=log,
                )
            except Exception:
                log.exception("state_age_bound_ending_failed")
                continue
            counts["parked" if bound.ending is WaitEnding.PARK else "failed"] += 1

    return counts


async def _end_expired_wait(  # noqa: PLR0913 — one ending's evidence, each part named
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    story: StoryDTO,
    bound: StateAgeBound,
    anchor: datetime,
    age_minutes: float,
    threshold_minutes: int,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Record the expiry, tell the owner, and leave the status the scan reads.

    The order is the one every terminal supervisor ending uses: the typed reason
    first, then the durable owner record, then the transition, then the delivery
    and the administrator notice. It is not ours to reorder — see
    `tasks/owner_notifications.py` for why the record precedes the commit.

    The transition is what makes this idempotent: an expired story leaves the
    status this watchdog scans, so the next tick cannot park, owe or notify it a
    second time. Nothing is owed on any path that does not transition.
    """
    project_id = str(story.project_id)
    reason = {
        "reason": STATE_AGE_BOUND_REASON,
        "status": bound.status.value,
        "waiting_on": WAITING_ON_BY_STATUS[bound.status].value,
        "config_key": bound.config_key,
        "threshold_minutes": threshold_minutes,
        "anchor": bound.anchor,
        "anchor_at": anchor.isoformat(),
        "age_minutes": round(age_minutes, 1),
        "ending": bound.ending.value,
    }
    log.error("state_age_bound_expired", **reason)
    await api_client.update_story(story.id, {"quarantine_reason": reason})
    owed = await owe_story_owner_notification(
        api_client,
        story.id,
        event=_OWNER_EVENT_BY_ENDING[bound.ending],
        text=bound.owner_text(threshold_minutes),
        project_id=project_id,
        terminal_status=_TERMINAL_STATUS_BY_ENDING[bound.ending],
        log=log,
    )
    if bound.ending is WaitEnding.PARK:
        await api_client.transition_story(story.id, STORY_HUMAN_REVIEW_ACTION)
    else:
        await api_client.fail_story(story.id)
    await deliver_owed_notification(
        api_client, redis_client, story.id, owed, log, story_record=True
    )
    await notify_admins_best_effort(
        f"Story {story.id} waited in {bound.status.value} for "
        f"{reason['age_minutes']} minutes, past the {threshold_minutes}-minute bound measured "
        f"from {bound.anchor}",
        level="error",
        story_id=story.id,
        project_id=project_id,
    )
