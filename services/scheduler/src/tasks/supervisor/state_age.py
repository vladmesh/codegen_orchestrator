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

The watchdog reads first and ends a wait later, and routing may move the story
on in between. So an ending is a compare-and-set the API applies on the locked
rows (`shared/contracts/dto/state_wait.py`): the watchdog sends the status it
read and the identity of the anchor it measured, and a story that is no longer
there is skipped and logged, never parked or failed. Nothing here depends on
where the dispatcher calls it.

What a bound is measured from is the part that has to be defensible, so each
entry names its own anchor instead of sharing the Story row's ``updated_at``,
which any unrelated write moves:

* ``deploying`` / ``testing`` — the in-flight Run's own ``created_at``. Only a
  QUEUED or RUNNING run is a wait at all; a terminal run is routed by the deploy
  and QA supervisors on the same tick. A re-dispatch is a *new* Run, so the
  bound restarts exactly when the platform genuinely started over, and nothing
  else can reset it.
* ``waiting_user_secret`` — the moment the request for the secrets was
  *delivered* to the owner, read from the durable owner-notification record of
  the ask (``delivered_at``) and from nowhere else. The invariant is that the
  clock starts only when the owner has been told, so every state of that record
  has one meaning here: delivered starts the clock; owed means delivery is still
  being retried and the clock has not started; unaddressable or abandoned means
  the owner was never asked, so the story is never failed as unanswered; and no
  ask record at all (a wait entered before the ask went through the seam) is
  asked now, once, and its clock starts at that delivery. Not the run's own
  timestamps, and not ``owed_at``: those are moments *before* anybody was told.
* ``pr_review`` — the pull request's own ``updated_at`` on GitHub. It lives
  outside this process, so it survives a tick restart; it moves when the pull
  request moves (a push, an update-branch, the merge) and not when something
  unrelated writes the story row. A pull request nothing is doing anything to is
  precisely one whose ``updated_at`` stands still.

* ``in_progress`` with no task in its current work cycle — the Story row's own
  ``updated_at``, which is when it entered ``in_progress`` as long as nothing
  wrote it since. Such a story was taken by an architect that never planned
  it: the scaffold it waited for failed or never finished, or the architect
  died. Nothing else moves it — ``supervise_stuck_stories`` retries only
  ``created`` — so without this bound it reads "being built" for ever. A story
  whose Product Brief still has a live planning attempt is left to its planner,
  and the API refuses the ending if the row was written or a task appeared.
  This bound ends the wait; it is not the stage's expected duration, so the
  stage notices keep calling ``in_progress`` unbounded.

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
from typing import TYPE_CHECKING

from pydantic import ValidationError
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.run import RunType
from shared.contracts.dto.state_wait import (
    IN_FLIGHT_RUN_STATUSES,
    OWNER_EVENT_BY_ENDING,
    TERMINAL_STATUS_BY_ENDING,
    StateWaitAnchor,
    StateWaitEnding,
    StateWaitExpiryCommand,
    StateWaitExpiryDisposition,
    StateWaitExpiryReason,
    StateWaitSkip,
    StateWaitSkipReason,
)
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.dto.story_failure import in_work_cycle
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import notify_admins_best_effort
from shared.redis import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from .._github_refs import _parse_github_timestamp, _parse_owner_repo
from ..owner_notifications import (
    deliver_owed_notification,
    new_story_owner_notification,
    read_owner_notification,
)
from .common import _parse_datetime
from .deploy import deliver_user_secret_request, owe_user_secret_request
from .liveness import _planning_attempt_is_live

logger = structlog.get_logger(__name__)

#: The typed reason a `waiting_user_secret` story carries when its request never
#: reached the owner. It names the undelivered request, not a timeout: the clock
#: of this wait never started, so nothing about it expired.
USER_SECRET_REQUEST_UNDELIVERED_REASON = "user_secret_request_undelivered"  # noqa: S105

#: Ask-record states in which the owner was never told and never will be through
#: this path. The seam has already told administrators about each.
_UNDELIVERED_ASK_STATES = frozenset(
    {OwnerNotificationState.UNADDRESSABLE, OwnerNotificationState.ABANDONED}
)


@dataclass(frozen=True)
class _Sweep:
    """What one pass of the watchdog gives every anchor resolver."""

    api_client: SchedulerAPIClient
    redis_client: RedisStreamClient
    github: GitHubAppClient


@dataclass(frozen=True)
class _Anchor:
    """Where a wait's age is measured from, and which record that moment belongs to.

    ``identity`` is what the ending sends back to the API, so the wait it ends
    is the wait whose age was measured and not whatever replaced it since.
    """

    at: datetime
    identity: StateWaitAnchor


AnchorResolver = Callable[[_Sweep, StoryDTO], Awaitable[_Anchor | None]]
#: The last look at an anchor that lives outside the API, taken immediately
#: before the ending is asked for: a named skip when it moved, else None.
AnchorConfirmation = Callable[[_Sweep, StoryDTO, _Anchor], Awaitable[StateWaitSkip | None]]


@dataclass(frozen=True)
class StateAgeBound:
    """One waiting state's bound: how long, measured from where, ending how."""

    status: StoryStatus
    config_key: str
    #: What the age is measured from, in the words the report and the typed
    #: reason use. Not a free-text log string: it is part of the evidence.
    anchor: str
    ending: StateWaitEnding
    resolve_anchor: AnchorResolver
    owner_text: Callable[[int], str]
    confirm_anchor: AnchorConfirmation | None = None
    #: Whether this bound is also the stage's expected duration, which the
    #: stage notices tell the owner. False for a bound on one shape of a stage
    #: only (a planless ``in_progress``), which says nothing about the stage.
    bounds_stage: bool = True


def _threshold_minutes(bound: StateAgeBound) -> int:
    return startup.get_config().get_int(bound.config_key)


def _age_minutes(moment: datetime) -> float:
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - reference).total_seconds() / 60


async def _in_flight_run_anchor(
    api_client: SchedulerAPIClient,
    story: StoryDTO,
    run_type: RunType,
) -> _Anchor | None:
    """When the Run this story is waiting on started, or None if it is not waiting."""
    log = logger.bind(story_id=story.id, run_type=run_type.value)
    try:
        run = await api_client.get_latest_run_by_story(story.id, run_type=run_type.value)
    except ValidationError:
        # The routing supervisor reads the same run on this tick and fails the
        # story on it loudly. Nothing to bound and nothing to hide.
        log.info("state_age_bound_unreadable_run")
        return None
    if run is None or run.status not in IN_FLIGHT_RUN_STATUSES:
        return None
    return _Anchor(at=_parse_datetime(run.created_at), identity=StateWaitAnchor(run_id=run.id))


async def _deploy_run_anchor(sweep: _Sweep, story: StoryDTO) -> _Anchor | None:
    return await _in_flight_run_anchor(sweep.api_client, story, RunType.DEPLOY)


async def _qa_run_anchor(sweep: _Sweep, story: StoryDTO) -> _Anchor | None:
    return await _in_flight_run_anchor(sweep.api_client, story, RunType.QA)


async def _user_secret_request_anchor(sweep: _Sweep, story: StoryDTO) -> _Anchor | None:
    """When the request for the missing secrets was delivered to the owner.

    Resolved from the ask's owner-notification record alone — the record on the
    deploy Run that reported the missing keys, which is the record of exactly
    this wait. Only a delivered ask has an anchor; every other state answers
    None, so the story cannot expire, and two of them act:

    * no ask record — a wait entered before the ask went through the seam. It is
      owed and delivered now, once; `owe_user_secret_request` returns the record
      already there on every later tick, so it is never owed a second time;
    * unaddressable / abandoned — the owner was never told. The story carries a
      typed reason naming the undelivered request, written once, and an
      administrator is told the wait will not end on its own.

    An owed record needs nothing from here: the owner-notification sweep selects
    owed Run records whatever their story's status and retries them.
    """
    api_client = sweep.api_client
    log = logger.bind(story_id=story.id)
    try:
        run = await api_client.get_latest_run_by_story(story.id, run_type=RunType.DEPLOY.value)
    except ValidationError:
        log.info("state_age_bound_unreadable_run", run_type=RunType.DEPLOY.value)
        return None
    if run is None or run.result is None or not run.result.missing_user_secrets:
        return None
    project_id = str(story.project_id)
    log = log.bind(run_id=run.id)
    record = read_owner_notification(run)

    if record is None or record.state is OwnerNotificationState.VOIDED:
        record = await owe_user_secret_request(api_client, run, story.id, project_id, log)
        log.info("state_age_bound_user_secret_request_owed_for_existing_wait")
        await deliver_user_secret_request(api_client, sweep.redis_client, run, record, log)
        return None
    if record.event is not OwnerNotificationEvent.STORY_WAITING_USER_SECRET:
        # Only the ask is ever owed on a Run whose outcome is a secret wait; any
        # other record here is a defect. Never expire on it, and say so loudly.
        log.error("state_age_bound_user_secret_run_carries_other_record", po_event=record.event)
        return None
    if record.state is OwnerNotificationState.DELIVERED:
        if record.delivered_at is None:
            log.error("state_age_bound_user_secret_delivered_without_moment")
            return None
        return _Anchor(
            at=_parse_datetime(record.delivered_at),
            identity=StateWaitAnchor(run_id=run.id, ask_delivered_at=record.delivered_at),
        )
    if record.state in _UNDELIVERED_ASK_STATES:
        await _record_undelivered_request(sweep, story, run.id, record.state, record.detail, log)
    return None


async def _record_undelivered_request(  # noqa: PLR0913 — one undelivered ask's evidence
    sweep: _Sweep,
    story: StoryDTO,
    run_id: str,
    state: OwnerNotificationState,
    detail: str | None,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Name, once, a secret wait whose request never reached its owner.

    The story stays in `waiting_user_secret`: its only other transition is to
    fail it, and failing an owner for not answering a request they never got is
    the one message this bound must never send. It still resumes the moment the
    secrets are saved — by the owner through another channel, or by an operator
    — and the reason and notice below tell an operator it will not end alone.
    """
    reason = story.quarantine_reason or {}
    if (
        reason.get("reason") == USER_SECRET_REQUEST_UNDELIVERED_REASON
        and reason.get("run_id") == run_id
    ):
        return
    undelivered = {
        "reason": USER_SECRET_REQUEST_UNDELIVERED_REASON,
        "status": StoryStatus.WAITING_USER_SECRET.value,
        "run_id": run_id,
        "delivery_state": state.value,
        "detail": detail,
    }
    log.error("state_age_bound_user_secret_request_undelivered", **undelivered)
    await sweep.api_client.update_story(story.id, {"quarantine_reason": undelivered})
    await notify_admins_best_effort(
        f"Story {story.id} is waiting for user secrets, but the request never reached its "
        f"owner ({state.value}: {detail}). The wait has no clock and will not end on its "
        "own: save the secrets for the owner, or fail the story.",
        level="error",
        story_id=story.id,
        project_id=str(story.project_id),
    )


async def _pull_request_anchor(sweep: _Sweep, story: StoryDTO) -> _Anchor | None:
    """When this story's pull request last moved, as GitHub records it."""
    api_client, github = sweep.api_client, sweep.github
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
    moved_at = _parse_github_timestamp(pull_request.get("updated_at")) or _parse_github_timestamp(
        pull_request.get("created_at")
    )
    if moved_at is None:
        return None
    return _Anchor(at=moved_at, identity=StateWaitAnchor(pr_number=story.pr_number))


async def _confirm_pull_request_anchor(
    sweep: _Sweep, story: StoryDTO, anchor: _Anchor
) -> StateWaitSkip | None:
    """Read the pull request again; a wait whose PR moved since is not ended.

    The API can lock ``pr_number`` but not GitHub, so this re-read immediately
    before the ending is the closest check an external anchor allows. A read
    that fails now is not an observation either, so it skips too.
    """
    current = await _pull_request_anchor(sweep, story)
    if current is not None and current.at == anchor.at:
        return None
    return StateWaitSkip(
        reason=StateWaitSkipReason.PR_UPDATED_AT_MOVED,
        expected=anchor.at.isoformat(),
        actual=None if current is None else current.at.isoformat(),
    )


async def _planless_story_anchor(sweep: _Sweep, story: StoryDTO) -> _Anchor | None:
    """When a story with no plan entered ``in_progress``, or None if it has a plan.

    A task of the current work cycle is a plan, however far it got; a Product
    Brief whose planning attempt is still heartbeating is a plan being made.
    Either answers None, so the story is left to the supervisors that own it.
    """
    api_client = sweep.api_client
    tasks = await api_client.get_tasks_by_story(story.id)
    if any(in_work_cycle(task.created_at, story.reopened_at, task.status) for task in tasks):
        return None
    brief = await api_client.get_product_brief_by_story(story.id)
    if brief is not None and _planning_attempt_is_live(brief, datetime.now(UTC)):
        return None
    if story.updated_at is None:
        return None
    return _Anchor(
        at=_parse_datetime(story.updated_at),
        identity=StateWaitAnchor(story_updated_at=story.updated_at),
    )


def _planless_owner_text(threshold_minutes: int) -> str:
    return (
        "Work on this change has not started: after over "
        f"{threshold_minutes} minutes no development task was created for it, so it "
        "is not going to move on its own. Work is stopped and a person has to look at "
        "it; nothing more happens automatically."
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
        # 30 min: `stand_deadlines.DEPLOY_TIMEOUT` (420 s) plus
        # `DEPLOY_OUTCOME_TIMEOUT` (120 s) is a live deploy Run, ~9 min; the
        # default is a generous multiple of that for a slow host.
        status=StoryStatus.DEPLOYING,
        config_key="supervisor.deploy_wait_max_minutes",
        anchor="deploy_run_created_at",
        ending=StateWaitEnding.PARK,
        resolve_anchor=_deploy_run_anchor,
        owner_text=_deploy_owner_text,
    ),
    StateAgeBound(
        # 60 min: `stand_deadlines.QA_RUN_TIMEOUT` is 300 s, and the identity a
        # QA run borrows is revoked at `supervisor.temporary_access_ttl_minutes`
        # (60), past which the run cannot test anything anyway.
        status=StoryStatus.TESTING,
        config_key="supervisor.qa_wait_max_minutes",
        anchor="qa_run_created_at",
        ending=StateWaitEnding.PARK,
        resolve_anchor=_qa_run_anchor,
        owner_text=_qa_owner_text,
    ),
    StateAgeBound(
        # 220 min: ten times `stand_deadlines.DEPLOY_RUN_TIMEOUT` (1320 s, the
        # gate's own ceiling for merge to deploy Run, which already contains the
        # 900 s image window): 10 * 1320 s / 60 = 220. Absorbs a queued GitHub
        # Actions and still surfaces a stuck story the same day.
        status=StoryStatus.PR_REVIEW,
        config_key="supervisor.pr_review_wait_max_minutes",
        anchor="github_pull_request_updated_at",
        ending=StateWaitEnding.PARK,
        resolve_anchor=_pull_request_anchor,
        owner_text=_pr_review_owner_text,
        confirm_anchor=_confirm_pull_request_anchor,
    ),
    StateAgeBound(
        # 1440 min: a wait on a person, so it takes the number the pipeline
        # already uses for its other wait on one,
        # `supervisor.resource_wait_timeout_minutes`.
        status=StoryStatus.WAITING_USER_SECRET,
        config_key="supervisor.user_secret_wait_max_minutes",
        anchor="user_secret_request_delivered_at",
        ending=StateWaitEnding.FAIL,
        resolve_anchor=_user_secret_request_anchor,
        owner_text=_user_secret_owner_text,
    ),
    StateAgeBound(
        # 60 min: the architect waits `SCAFFOLD_WAIT_MAX` (5 min) for the
        # repository and then plans in one LLM run of minutes; a Product Brief
        # plan in progress is excluded by its own heartbeat. An hour without a
        # single task is not a slow plan, it is no plan.
        status=StoryStatus.IN_PROGRESS,
        config_key="supervisor.planless_story_max_minutes",
        anchor="story_updated_at_without_tasks",
        ending=StateWaitEnding.PARK,
        resolve_anchor=_planless_story_anchor,
        owner_text=_planless_owner_text,
        bounds_stage=False,
    ),
)


def configured_bound_minutes(status: StoryStatus) -> int | None:
    """The configured upper bound of a Story state's wait, if the map bounds it.

    The stage notices read their order-of-magnitude estimate from here, so the
    number an owner is told about and the number that ends the wait are one.
    """
    for bound in STATE_AGE_BOUNDS:
        if bound.status is status and bound.bounds_stage:
            return _threshold_minutes(bound)
    return None


async def supervise_state_age_bounds(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Apply every state age bound once, and end the waits that are over.

    A wait is ended only through the API's compare-and-set: the story must still
    be in the status this pass read, with the anchor its age was measured from.
    Routing that moved the story on in between — in this tick or another loop,
    before or after this call — makes the ending a logged skip that writes
    nothing, so this watchdog is correct wherever it runs.

    Returns the number of stories parked and failed by the bounds this pass, and
    of expired waits skipped because the story had moved on.
    """
    counts = {"parked": 0, "failed": 0, "skipped": 0}
    sweep = _Sweep(api_client=api_client, redis_client=redis_client, github=GitHubAppClient())

    for bound in STATE_AGE_BOUNDS:
        threshold = _threshold_minutes(bound)
        for story in await api_client.get_stories_by_status(bound.status):
            log = logger.bind(
                story_id=story.id,
                project_id=str(story.project_id),
                status=bound.status.value,
            )
            try:
                anchor = await bound.resolve_anchor(sweep, story)
            except Exception:
                # Without an anchor there is no bound to apply, and one story's
                # unreadable evidence must not stop the rest of the sweep.
                log.exception("state_age_bound_anchor_failed")
                continue
            if anchor is None:
                continue
            age = _age_minutes(anchor.at)
            if age < threshold:
                continue
            # One story's ending must not stop the others: they are unrelated
            # work, and a sweep that dies on the first broken story leaves every
            # later one waiting exactly as long as the failure lasts.
            try:
                disposition = await _end_expired_wait(
                    api_client,
                    redis_client,
                    sweep=sweep,
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
            if disposition is StateWaitExpiryDisposition.SKIPPED:
                counts["skipped"] += 1
            elif disposition is StateWaitExpiryDisposition.EXPIRED:
                counts["parked" if bound.ending is StateWaitEnding.PARK else "failed"] += 1

    return counts


def _log_skip(
    log: structlog.stdlib.BoundLogger,
    bound: StateAgeBound,
    skip: StateWaitSkip,
    actual_status: StoryStatus,
) -> None:
    log.info(
        "state_age_bound_skipped",
        expected_status=bound.status.value,
        actual_status=actual_status.value,
        mismatch=skip.reason.value,
        expected=skip.expected,
        actual=skip.actual,
        anchor=bound.anchor,
    )


async def _end_expired_wait(  # noqa: PLR0913 — one ending's evidence, each part named
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    sweep: _Sweep,
    story: StoryDTO,
    bound: StateAgeBound,
    anchor: _Anchor,
    age_minutes: float,
    threshold_minutes: int,
    log: structlog.stdlib.BoundLogger,
) -> StateWaitExpiryDisposition:
    """End the wait if it is still the one that expired; then tell the owner.

    The typed reason, the owed owner record and the transition are one API
    action that checks the status and anchor on the locked rows first
    (`POST /stories/{id}/expire-state-wait`). Only an ending that committed is
    delivered and told to administrators; a skip writes and tells nothing. The
    record commits with its transition, so `tasks/owner_notifications.py`'s
    order — record before the transition is observable — holds by construction.

    The transition is what makes this idempotent: an expired story leaves the
    status this watchdog scans, and a repeat of an ending that already committed
    is answered `already_ended`, with nothing owed a second time.
    """
    project_id = str(story.project_id)
    reason = StateWaitExpiryReason(
        status=bound.status,
        waiting_on=WAITING_ON_BY_STATUS[bound.status].value,
        config_key=bound.config_key,
        threshold_minutes=threshold_minutes,
        anchor=bound.anchor,
        anchor_at=anchor.at.isoformat(),
        age_minutes=round(age_minutes, 1),
        ending=bound.ending,
    )
    owed = new_story_owner_notification(
        story.id,
        event=OWNER_EVENT_BY_ENDING[bound.ending],
        text=bound.owner_text(threshold_minutes),
        project_id=project_id,
        terminal_status=TERMINAL_STATUS_BY_ENDING[bound.ending],
    )
    if bound.confirm_anchor is not None:
        moved = await bound.confirm_anchor(sweep, story, anchor)
        if moved is not None:
            _log_skip(log, bound, moved, bound.status)
            return StateWaitExpiryDisposition.SKIPPED

    ended = await api_client.expire_state_wait(
        story.id,
        StateWaitExpiryCommand(
            expected_status=bound.status,
            ending=bound.ending,
            anchor=anchor.identity,
            reason=reason,
            owner_notification=owed,
        ),
    )
    if ended.disposition is StateWaitExpiryDisposition.SKIPPED:
        _log_skip(log, bound, ended.skip, ended.story_status)
        return ended.disposition
    if ended.disposition is StateWaitExpiryDisposition.ALREADY_ENDED:
        log.info("state_age_bound_already_ended", story_status=ended.story_status.value)
        return ended.disposition

    log.error("state_age_bound_expired", **reason.model_dump(mode="json"))
    await deliver_owed_notification(
        api_client, redis_client, story.id, owed, log, story_record=True
    )
    await notify_admins_best_effort(
        f"Story {story.id} waited in {bound.status.value} for "
        f"{reason.age_minutes} minutes, past the {threshold_minutes}-minute bound measured "
        f"from {bound.anchor}",
        level="error",
        story_id=story.id,
        project_id=project_id,
    )
    return ended.disposition
