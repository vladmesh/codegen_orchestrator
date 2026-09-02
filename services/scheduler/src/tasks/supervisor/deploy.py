"""Deploy outcome, refusal, recovery, and user-secret supervision."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
import uuid

from pydantic import ValidationError
import structlog

from shared.allocation_disposition import (
    PlacementPath,
    RefusalRouting,
    attempt_disposition,
    may_terminate_story,
    refusal_routing,
)
from shared.contracts.dto.project import (
    ProjectPredatesRunOwnership,
    require_initiating_run,
)
from shared.contracts.dto.qa_handoff import (
    QA_HANDOFF_KEY,
    QAHandoffPlan,
    TemporaryAccessRequest,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import (
    DeployRunResult,
)
from shared.contracts.dto.settings_seed import SETTINGS_SEED_RETRYABLE_FAILURES
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.users_grant import (
    USERS_GRANT_INTENT_KEY,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
)
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployOutcome,
    DeployTrigger,
)
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.queues.qa import QAMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import (
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
)
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from .._recipients import resolve_project_recipient
from ..owner_notifications import (
    deliver_owed_notification,
    owe_owner_notification,
)
from .common import (
    STORY_HUMAN_REVIEW_ACTION,
    _admissible_target_exists,
    _fail_story_on_invalid_result,
    _notify_admin_failure,
    _parse_datetime,
    _qa_handoff_recovery_minutes,
    _resource_wait_timeout_minutes,
)
from .handoff import (
    _execute_qa_handoff,
    _qa_run_id_for_deploy,
    _resolve_qa_repository,
    _temporary_access_is_needed,
)

logger = structlog.get_logger(__name__)
DEPLOY_RETRY_KEY_PREFIX = "deploy:retries:"

#: Where a deploy that carried an infrastructure wait forward started waiting.
#: Stored in `run_metadata` so the bound survives every re-dispatch.
INFRASTRUCTURE_WAIT_STARTED_KEY = "infrastructure_wait_started_at"
RECHECK_DEPLOY_MESSAGE_KEY = "recheck_message"
RECHECK_DEPLOY_DISPATCHED_AT_KEY = "recheck_deploy_dispatched_at"

# A Run which failed before its EngineeringMessage reached the queue is terminal
# evidence for recovery, but never provider work. The reservation is released
# before this status update so terminal finalization cannot turn it into an
# unknown-cost hold.
DEPLOY_FIX_PRE_HANDOFF_PREPARATION_FAILED_ERROR = "deploy-fix handoff preparation failed"

# The durable owner-notification record lives on the failed deploy Run, which
# remains available after the denied fix parks the story in human review.
ENGINEERING_BUDGET_DENIED_TEXT = (
    "Engineering cannot start the deploy fix because this project's engineering budget is "
    "currently exhausted. Tell the user that the work is waiting for their review."
)


class RefusedDeployAction(StrEnum):
    """What the deploy path did with one refused placement, for the tick counts.

    Three outcomes, because the shared table gives the refusal dispositions three
    behaviours. An escalation counted as a wait would be the same collapse in the
    reporting that the routing no longer allows.
    """

    REDISPATCHED = "redispatched"
    WAITING = "waiting"
    ESCALATED = "escalated"
    FAILED = "failed"


class DeployRetryAction(StrEnum):
    """One retry tick's truthful outcome.

    ``RETRIED`` means a fresh Run was dispatched. ``RECONCILED`` means the
    service-side completion won but its response was lost, so the source Run
    follows the normal successful-deploy handoff without consuming a retry.
    """

    RETRIED = "retried"
    RECONCILED = "reconciled"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"


def _max_deploy_retries() -> int:
    return startup.get_config().get_int("deploy.max_deploy_retries")


def _max_deploy_fix_attempts() -> int:
    return startup.get_config().get_int("deploy.max_deploy_fix_attempts")


def _deploy_retry_ttl() -> int:
    return startup.get_config().get_int("deploy.deploy_retry_ttl")


#: The API exposes story transitions as action endpoints, not as status values:
#: `POST stories/{id}/human-review` is what moves a story into the human-review
#: queue. Posting `waiting_human_review` instead is a 404 — an escalation that
#: reaches nobody — so the action lives here once and every caller uses it.
async def supervise_deploying_stories(  # noqa: C901, PLR0912, PLR0915
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll DEPLOYING stories and route based on deploy run outcome.

    Reads run.result.deploy_outcome set by the deploy worker:
    - SUCCESS → story TESTING, publish QAMessage
    - SMOKE_FAILURE / CODE_FIX → story IN_PROGRESS, redispatch to engineering
    - RETRY → increment retry counter, re-publish DeployMessage or FAILED
    - WAITING_INFRASTRUCTURE → routed by `shared.allocation_disposition`, which
      gives each refusal disposition its own behaviour: a wait that resumes, an
      escalation to the human-review queue, or an operator alert. Never failed:
      the deploy never ran, because the platform had no server to run it on.
    - GIVE_UP → story FAILED, notify admins

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.DEPLOYING)
    if not stories:
        return {
            "tested": 0,
            "retried": 0,
            "redispatched": 0,
            "waiting": 0,
            "escalated": 0,
            "failed": 0,
        }

    tested = 0
    retried = 0
    redispatched = 0
    waiting = 0
    failed = 0
    refused: dict[RefusedDeployAction, int] = dict.fromkeys(RefusedDeployAction, 0)
    redis = redis_client._redis

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        # Find latest deploy run for this story
        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="deploy")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(
                api_client, story_id, project_id, "deploy", exc, log
            )
            failed += 1
            continue
        if run is None:
            continue

        # A recheck deploy persists the exact message before publication. A
        # process can die after that commit, so a queued recheck without its
        # dispatch stamp is recovered here instead of remaining DEPLOYING
        # forever. Other queued deploys have no reconstructable handoff.
        if run.status is RunStatus.QUEUED:
            if await _recover_recheck_deploy_handoff(api_client, redis_client, run, log):
                retried += 1
            continue
        if run.status is RunStatus.RUNNING:
            continue

        # Only a superseded (CANCELLED) run reaches here without a result; a
        # terminal run that lost its outcome would have failed validation above.
        if run.result is None:
            log.info("deploy_run_superseded_skip", run_id=run.id, run_status=run.status.value)
            continue

        outcome = run.result.deploy_outcome

        if outcome == DeployOutcome.SUCCESS:
            handed_off = await _handle_deploy_success_story(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            if handed_off:
                tested += 1
            else:
                failed += 1

        elif outcome in (DeployOutcome.CODE_FIX, DeployOutcome.SMOKE_FAILURE):
            dispatched = await _handle_deploy_code_fix(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            if dispatched:
                redispatched += 1
            else:
                failed += 1

        elif outcome in (
            DeployOutcome.RETRY,
            DeployOutcome.CANCELLED,
            DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
        ):
            # A cancelled deploy did not fail and did not deploy: something took
            # the project away from it — the fence a temporary-access revoke
            # takes, or another deploy holding the lock. The story still needs
            # its commit deployed, so it goes round again under the same bound
            # that stops a failing deploy from looping.
            if outcome is DeployOutcome.CANCELLED:
                log.info("deploy_supervisor_redeploy_after_cancel", run_id=run.id)
            retry_action = await _handle_deploy_retry(
                api_client, redis_client, redis, story_id, project_id, run, log
            )
            if retry_action is DeployRetryAction.RETRIED:
                retried += 1
            elif retry_action is DeployRetryAction.RECONCILED:
                tested += 1
            elif retry_action is DeployRetryAction.FAILED:
                failed += 1

        elif outcome is DeployOutcome.SETTINGS_SEED_FAILED:
            # The application is up; a confirmed setting of the story's brief is
            # not in it. This is its own route on purpose — the retry branch
            # above reconciles an applied owner grant straight to SUCCESS, which
            # would hand QA a deploy presented as successful with the readback
            # evidence gone.
            retry_action = await _handle_settings_seed_retry(
                api_client, redis_client, redis, story_id, project_id, run, log
            )
            if retry_action is DeployRetryAction.RETRIED:
                retried += 1
            else:
                failed += 1

        elif outcome is DeployOutcome.WAITING_INFRASTRUCTURE:
            action = await _route_refused_deploy(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            # Each action names the counter it advances, so a behaviour the
            # routing distinguishes cannot be merged back together in the counts.
            refused[action] += 1

        elif outcome == DeployOutcome.WAITING_FOR_USER_SECRET:
            await _handle_deploy_waiting_user_secret(
                api_client, redis_client, story_id, project_id, run, log
            )
            waiting += 1

        elif outcome in (
            DeployOutcome.GIVE_UP,
            DeployOutcome.ALLOCATION_MISSING,
            DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
            DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED,
            DeployOutcome.HEAD_SHA_MISSING,
        ):
            await _handle_deploy_give_up(api_client, story_id, project_id, run, log)
            failed += 1

    return {
        "tested": tested,
        "retried": retried,
        "redispatched": redispatched + refused[RefusedDeployAction.REDISPATCHED],
        "waiting": waiting + refused[RefusedDeployAction.WAITING],
        "escalated": refused[RefusedDeployAction.ESCALATED],
        "failed": failed + refused[RefusedDeployAction.FAILED],
    }


async def _recover_recheck_deploy_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run,
    log: structlog.stdlib.BoundLogger,
) -> DeployRetryAction:
    """Publish a durable recheck deploy handoff left queued by a failed caller.

    The same age fence as QA handoff recovery leaves the original publisher time
    to stamp a successful publish, so a supervisor tick cannot duplicate its
    task id in that transaction-to-stamp window.
    """
    message_data = run.run_metadata.get(RECHECK_DEPLOY_MESSAGE_KEY)
    if message_data is None or run.run_metadata.get(RECHECK_DEPLOY_DISPATCHED_AT_KEY):
        return False

    age_minutes = (datetime.now(UTC) - _parse_datetime(run.created_at)).total_seconds() / 60
    if age_minutes < _qa_handoff_recovery_minutes():
        return False

    message = DeployMessage.model_validate(message_data)
    await redis_client.publish_message(DEPLOY_QUEUE, message)
    await api_client.update_run(
        run.id,
        {"run_metadata": {RECHECK_DEPLOY_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat()}},
    )
    log.warning("recheck_deploy_handoff_recovered", run_id=run.id)
    return True


async def _handle_deploy_success_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy succeeded — transition story to TESTING and start the QA run.

    A private bot admits QA only through the deploy-time test slot, so the run
    is started from a temporary access grant instead of directly: the grant is
    recorded, the value is deployed on the same commit, and the sweep releases
    the QA message once that deploy confirms. Everything after that, including
    taking the access back, follows from the record.

    Returns True if the story was handed off to QA, False if QA's preconditions
    were not met (handled as a visible failure).
    """
    deployed_url = result.deployed_url
    application_id = result.application_id

    # A QA handoff needs both the deployed URL and the application id. `application_id`
    # is legitimately optional on a DeployRunResult (a standalone deploy, or one where
    # the app record couldn't be resolved), so validate the precondition here — before
    # mutating story/run state — and route a success that can't reach QA to a visible
    # failure instead of crashing the tick mid-handoff.
    if deployed_url is None or application_id is None:
        missing = ", ".join(
            name
            for name, value in (("deployed_url", deployed_url), ("application_id", application_id))
            if value is None
        )
        log.error("deploy_success_missing_handoff_fields", missing=missing)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id, project_id, f"deploy reported success but missing {missing} — cannot run QA"
        )
        return False

    # QA validates the story against the repository's criteria, so resolve them
    # here and carry them on the message. Same reason as the fields above: a
    # story whose criteria are missing must not reach TESTING with a QA run that
    # can only error out.
    repo = await _resolve_qa_repository(api_client, project_id, log)
    if repo is None:
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but the project's repository has no acceptance criteria — "
            "cannot run QA",
        )
        return False

    acceptance_criteria = repo.acceptance_criteria.strip()

    # The bot username is persisted on the repository when the user's token is
    # validated, so QA gets it even when the deploy smoke check could not resolve
    # it via getMe. The smoke value is the older source and stays as a fallback
    # for projects whose token was stored before it was persisted.
    bot_username = repo.bot_username or result.bot_username

    # The access the QA identity needs is decided before anything moves, so a
    # story that cannot be granted it fails visibly instead of reaching TESTING
    # with a run that can only be refused by the bot.
    head_sha = _deploy_run_head_sha(run)
    # The project is read once here and used twice: it says who the bot admits,
    # and it carries the run that initiated this work, which the QA message has
    # to hand on so the QA executor is owned by the same run as the developer
    # workers that produced the code under test.
    project = await api_client.get_project(project_id)
    if project is None:
        log.error("qa_handoff_project_missing", project_id=project_id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id, project_id, "deploy succeeded but the project is gone — cannot run QA"
        )
        return False

    # A project that predates run ownership names no run, so its QA executor
    # could not be attributed once it dies. Fail the story rather than create an
    # unownable worker — the same refusal the API gives an admin.
    try:
        initiating_run_id = require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        log.error(
            "qa_handoff_project_has_no_initiating_run", project_id=project_id, reason=str(exc)
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but the project names no initiating run — cannot run QA",
        )
        return False

    grant_needed = _temporary_access_is_needed(project, result, log)
    if grant_needed and not head_sha:
        log.error("deploy_success_head_sha_missing_for_access_grant", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but its commit is unknown — QA cannot be granted temporary access",
        )
        return False

    # The QA run is created before the story leaves DEPLOYING, and it carries the
    # whole plan. Order matters in both directions: a crash before this leaves
    # the story where the deploy supervisor still sees it and this runs again,
    # and a crash after it leaves a run that says what was supposed to happen.
    # Its id is derived from the deploy run, so the retry lands on the same run
    # instead of creating a second one for the same deploy.
    qa_run_id = _qa_run_id_for_deploy(run.id)
    qa_recipient = await resolve_project_recipient(
        api_client, project_id, event="qa_dispatch", story_id=story_id
    )
    qa_message = QAMessage(
        story_id=story_id,
        project_id=project_id,
        initiating_run_id=initiating_run_id,
        telegram_chat_id=qa_recipient.telegram_chat_id,
        deployed_url=deployed_url,
        application_id=application_id,
        acceptance_criteria=acceptance_criteria,
        bot_username=bot_username,
        run_id=qa_run_id,
    )
    plan = QAHandoffPlan(
        qa_message=qa_message,
        access=TemporaryAccessRequest(
            target_application_id=application_id,
            target_base_url=deployed_url,
            head_sha=head_sha,
        )
        if grant_needed
        else None,
    )
    started = await api_client.start_paid_run(
        PaidRunStartCommand(
            id=qa_run_id,
            type=RunType.QA,
            project_id=uuid.UUID(project_id),
            story_id=story_id,
            run_metadata={
                "application_id": application_id,
                "deploy_run_id": run.id,
                QA_HANDOFF_KEY: plan.model_dump(mode="json"),
            },
        )
    )
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        log.info(
            "qa_handoff_count_admission_refused",
            qa_run_id=qa_run_id,
            reason=(
                started.admission.reason.value if started.admission.reason is not None else None
            ),
        )
        if started.admission.message:
            owed = await owe_owner_notification(
                api_client,
                run,
                event=OwnerNotificationEvent.STORY_QUARANTINED,
                text=started.admission.message,
                story_id=story_id,
                project_id=project_id,
                terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
                log=log,
            )
            await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
            await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        return False
    await api_client.transition_story(story_id, "test")

    await _execute_qa_handoff(api_client, redis_client, qa_run_id, plan, log)
    return True


async def _handle_deploy_code_fix(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy failed with CODE_FIX — redispatch to engineering if retries remain.

    Returns True if redispatched, False if retries exhausted.
    """
    # A fix is another attempt inside the run that initiated the work, so the
    # message carries the project's run: the worker it spawns belongs to the
    # same run as the one whose deploy failed.
    project = await api_client.get_project(project_id)
    if project is None:
        log.error("deploy_fix_project_missing", project_id=project_id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(run.id, project_id, "deploy fix needs a project that is gone")
        return False

    # Same refusal as the QA handoff: no initiating run, no ownable worker, so
    # the fix attempt is not started at all rather than started unattributable.
    try:
        initiating_run_id = require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        log.error(
            "deploy_fix_project_has_no_initiating_run", project_id=project_id, reason=str(exc)
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "deploy fix needs a project that names its initiating run"
        )
        return False

    attempt = result.deploy_fix_attempt
    if attempt >= _max_deploy_fix_attempts():
        log.warning(
            "deploy_fix_retries_exhausted",
            attempt=attempt,
            max=_max_deploy_fix_attempts(),
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(run.id, project_id, "deploy fix retries exhausted")
        return False

    error_details = result.error_details or "unknown deploy error"
    fix_task_id = f"eng-deploy-fix-{run.id}-{attempt + 1}"
    try:
        started = await api_client.start_paid_run(
            PaidRunStartCommand(
                id=fix_task_id,
                type=RunType.ENGINEERING,
                project_id=project.id,
                task_id=fix_task_id,
                story_id=story_id,
                run_metadata={"deploy_fix_attempt": attempt + 1},
            )
        )
    except Exception:
        log.exception("deploy_fix_paid_start_failed", run_id=fix_task_id)
        return False
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        budget = started.engineering_budget
        reason = {
            "reason": (
                "engineering_budget_denied"
                if budget is not None
                else (started.admission.reason.value if started.admission.reason else "denied")
            ),
            "attempt_id": fix_task_id,
        }
        if budget is not None:
            reason.update(
                known_spend_microusd=budget.known_spend_microusd,
                active_held_microusd=budget.active_held_microusd,
                available_microusd=budget.available_microusd,
            )
        log.info("deploy_fix_admission_refused", **reason)
        await api_client.update_story(story_id, {"quarantine_reason": reason})
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text=started.admission.message or ENGINEERING_BUDGET_DENIED_TEXT,
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
        await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        return False

    try:
        # Transition story back to IN_PROGRESS only for an admitted handoff.
        await api_client.transition_story(story_id, "start")
        fix_recipient = await resolve_project_recipient(
            api_client, project_id, event="deploy_code_fix", story_id=story_id
        )
        fix_msg = EngineeringMessage(
            task_id=fix_task_id,
            project_id=project_id,
            initiating_run_id=initiating_run_id,
            telegram_chat_id=fix_recipient.telegram_chat_id,
            action="fix",
            description=(
                f"Deploy failed — fix the code so containers start cleanly.\n\n"
                f"Error: {error_details}\n\n"
                f"Run the service locally or check imports/dependencies before pushing."
            ),
            skip_deploy=False,
            story_id=story_id,
            deploy_fix_attempt=attempt + 1,
        )
    except Exception:
        # Preparation is demonstrably before any queue call, so this is safe to
        # abort. Keep publication outside this block: its outcome is unknowable.
        log.exception("deploy_fix_pre_handoff_preparation_failed", fix_task_id=fix_task_id)
        await api_client.abort_paid_run_pre_handoff(
            fix_task_id, DEPLOY_FIX_PRE_HANDOFF_PREPARATION_FAILED_ERROR
        )
        return False
    try:
        await redis_client.publish_message(ENGINEERING_QUEUE, fix_msg)
    except Exception:
        log.exception("deploy_fix_publish_outcome_unknown", fix_task_id=fix_task_id)
        return False
    log.info("deploy_supervisor_code_fix", fix_task_id=fix_task_id, attempt=attempt + 1)
    return True


async def _handle_deploy_retry(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    redis,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> DeployRetryAction:
    """Deploy failed with RETRY — re-publish deploy message if retries remain.

    Returns whether a new attempt was dispatched, an already-completed
    grant was reconciled, or ordinary recovery failed.
    """
    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        log.error("deploy_retry_head_sha_missing", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "deploy retry could not find original head_sha"
        )
        return DeployRetryAction.FAILED

    lifecycle = await _resume_initial_owner_intent(api_client, project_id, story_id, run, head_sha)
    if lifecycle is not None:
        if lifecycle.disposition is GrantIntentLifecycleDisposition.ALREADY_APPLIED:
            if run.result is None:
                return DeployRetryAction.FAILED
            success_result = _reconciled_success_result(run.result)
            if success_result is None:
                # The grant is applied, and this run records a confirmed setting
                # the product did not accept. Reconciling it would present the
                # deploy to QA as successful with that evidence gone, so it goes
                # round under the ordinary bound below instead.
                log.warning("deploy_supervisor_reconcile_refused_settings_seed", run_id=run.id)
                return await _redispatch_deploy_under_bound(
                    api_client, redis_client, redis, story_id, project_id, run, head_sha, log
                )
            reconciled = await _handle_deploy_success_story(
                api_client, redis_client, story_id, project_id, run, success_result, log
            )
            return DeployRetryAction.RECONCILED if reconciled else DeployRetryAction.FAILED
        if lifecycle.disposition is GrantIntentLifecycleDisposition.DISPATCHED:
            log.info(
                "deploy_supervisor_resumed_owner_intent",
                source_run_id=run.id,
                execution_run_id=lifecycle.execution_run_id,
            )
            return DeployRetryAction.RETRIED
        if lifecycle.disposition is GrantIntentLifecycleDisposition.EXHAUSTED:
            await _fail_exhausted_grant_intent(api_client, story_id, project_id, run, log)
            return DeployRetryAction.FAILED
        if lifecycle.disposition is GrantIntentLifecycleDisposition.STALE_TARGET:
            log.info("deploy_supervisor_owner_intent_stale_target", source_run_id=run.id)
        return DeployRetryAction.IN_FLIGHT

    return await _redispatch_deploy_under_bound(
        api_client, redis_client, redis, story_id, project_id, run, head_sha, log
    )


async def _handle_settings_seed_retry(  # noqa: PLR0913
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    redis,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> DeployRetryAction:
    """The product is up and refused, or did not prove, a confirmed setting.

    This route deliberately does not consult the initial-owner grant intent.
    The seed runs after that grant is applied, so the intent lifecycle can only
    answer ``ALREADY_APPLIED`` here and reconciling on it would turn a setting
    that never arrived into a successful deploy. The run goes round under the
    same ``deploy:retries:{story_id}`` bound that stops any failing deploy from
    looping; the seed itself is idempotent on ``(key, scope, subject_id)``.
    """
    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        log.error("deploy_settings_seed_retry_head_sha_missing", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "settings-seed retry could not find original head_sha"
        )
        return DeployRetryAction.FAILED
    log.warning(
        "deploy_supervisor_settings_seed_retry",
        run_id=run.id,
        failures=_held_back_settings_seed_failures(run.result),
    )
    return await _redispatch_deploy_under_bound(
        api_client, redis_client, redis, story_id, project_id, run, head_sha, log
    )


def _held_back_settings_seed_failures(result: DeployRunResult | None) -> list[str]:
    """The bounded failure kinds that hold this run's deploy back, deduplicated.

    Kinds only — never a key, a value or a capability, because this goes into a
    log line.
    """
    if result is None:
        return []
    return sorted(
        {
            outcome.failure.value
            for outcome in result.settings_seed
            if outcome.failure in SETTINGS_SEED_RETRYABLE_FAILURES
        }
    )


def _reconciled_success_result(result: DeployRunResult) -> DeployRunResult | None:
    """This run's result as a SUCCESS, or ``None`` if it may not become one.

    ``DeployRunResult`` holds the invariant that a success cannot carry a
    settings-seed failure that holds the deploy back, so re-validating here —
    rather than ``model_copy``, which runs no validators — is what makes this
    reconciliation reach it. Any future reconciliation that builds a success
    the same way inherits the check instead of having to remember it.
    """
    try:
        return DeployRunResult.model_validate(
            result.model_dump() | {"deploy_outcome": DeployOutcome.SUCCESS, "error_details": None}
        )
    except ValidationError:
        return None


async def _redispatch_deploy_under_bound(  # noqa: PLR0913
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    redis,
    story_id: str,
    project_id: str,
    run,
    head_sha: str,
    log: structlog.stdlib.BoundLogger,
) -> DeployRetryAction:
    """Deploy the same commit again, or fail the story once the bound is spent."""
    retry_key = f"{DEPLOY_RETRY_KEY_PREFIX}{story_id}"
    attempts = await redis.incr(retry_key)
    await redis.expire(retry_key, _deploy_retry_ttl())

    if attempts >= _max_deploy_retries():
        log.warning(
            "deploy_max_retries_exceeded",
            story_id=story_id,
            attempts=attempts,
            max_retries=_max_deploy_retries(),
        )
        await api_client.fail_story(story_id)
        await redis.delete(retry_key)
        await _notify_admin_failure(run.id, project_id, f"deploy retries exhausted ({attempts})")
        return DeployRetryAction.FAILED

    # Re-publish deploy message for retry
    new_run_id = f"deploy-retry-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "supervisor_retry",
                "attempt": attempts,
                "head_sha": head_sha,
            },
        }
    )

    retry_recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_retry", story_id=story_id
    )
    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        telegram_chat_id=retry_recipient.telegram_chat_id,
        unaddressed_reason=retry_recipient.unaddressed_reason,
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
        head_sha=head_sha,
    )
    await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
    log.info(
        "deploy_supervisor_retry",
        new_run_id=new_run_id,
        attempt=attempts,
        max_retries=_max_deploy_retries(),
    )
    return DeployRetryAction.RETRIED


async def _handle_deploy_give_up(
    api_client: SchedulerAPIClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Deploy failed with GIVE_UP — terminal failure, admin notified."""
    log.warning("deploy_supervisor_give_up", run_id=run.id)
    await api_client.fail_story(story_id)
    error_msg = (run.result.error_details if run.result else None) or "unknown error"
    await _notify_admin_failure(run.id, project_id, error_msg)


def _deploy_run_head_sha(run) -> str | None:
    """Read the exact commit a deploy run targeted, from its run_metadata."""
    run_metadata = getattr(run, "run_metadata", None) or {}
    return run_metadata.get("head_sha")


async def _resume_initial_owner_intent(
    api_client: SchedulerAPIClient, project_id: str, story_id: str, run, head_sha: str
) -> GrantIntentLifecycleResult | None:
    """Recover only the API-owned initial-owner intent referenced by this run."""
    intent_id = (getattr(run, "run_metadata", None) or {}).get(USERS_GRANT_INTENT_KEY)
    if not isinstance(intent_id, str) or not intent_id.startswith("users-grant-initial_owner-"):
        return None
    return GrantIntentLifecycleResult.model_validate(
        await api_client.resume_initial_owner_grant(
            project_id, story_id=story_id, head_sha=head_sha
        )
    )


async def _fail_exhausted_grant_intent(
    api_client: SchedulerAPIClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Turn API admission exhaustion into the ordinary terminal story outcome."""
    detail = "grant intent deployment retries exhausted"
    log.warning("deploy_grant_intent_retries_exhausted", run_id=run.id)
    await api_client.fail_story(story_id)
    await _notify_admin_failure(run.id, project_id, detail)


async def _route_refused_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> RefusedDeployAction:
    """Give the refusal the one behaviour the shared table owes its disposition.

    The classification is read from the run result rather than re-derived, and
    the behaviour comes from `shared.allocation_disposition` — the same table the
    engineering path consults — so this branch can neither treat a refusal as a
    product failure nor answer two dispositions the same way. Collapsing them is
    what left a request no server could ever fit polling forever with nobody
    told. The contract already refuses a `WAITING_INFRASTRUCTURE` result without
    its reason and budget, so both are present here.
    """
    reason = result.allocation_failure_reason
    disposition = attempt_disposition(reason, product_failure=True)
    routing = refusal_routing(PlacementPath.DEPLOY, disposition)
    log = log.bind(reason=reason.value, disposition=disposition.value, routing=routing.value)

    if routing is RefusalRouting.WAIT_FOR_ADMISSIBLE_TARGET:
        return await _handle_deploy_infrastructure_wait(
            api_client, redis_client, story_id, project_id, run, result, log
        )
    if routing is RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE:
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=True,
            detail=(
                f"deploy needs {result.allocation_required_ram_mb} MB RAM and "
                f"{result.allocation_min_disk_mb} MB disk, which exceeds every managed server"
            ),
            log=log,
        )
        return RefusedDeployAction.ESCALATED
    if routing is RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT:
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail=f"deploy placement could not be evaluated: {reason.value}",
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    # CALLER_FAILURE_ROUTING / NO_REFUSAL cannot be reached from an allocation
    # refusal while the shared table classifies every reason as infrastructure —
    # `may_terminate_story` says the same thing from the other side. If that ever
    # changes, a human hears about it: waiting would hide it and failing the
    # story would charge the platform's own mistake to the user's project.
    log.error(
        "deploy_refusal_misclassified",
        run_id=run.id,
        may_terminate_story=may_terminate_story(disposition),
    )
    await _escalate_refused_deploy(
        api_client,
        redis_client,
        story_id,
        project_id,
        run,
        result,
        tell_owner=False,
        detail=f"deploy refusal {reason.value} classified as {disposition.value}",
        log=log,
    )
    return RefusedDeployAction.ESCALATED


async def _escalate_refused_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    *,
    tell_owner: bool,
    detail: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Hand a refusal no wait can resolve to the human-review queue that exists.

    This is the same queue a quarantined QA story reaches, entered the same way:
    the reason is recorded on the story first, then the `human-review` action
    moves it. It is deliberately not `fail_story` — an infrastructure refusal is
    never evidence that the user's project is broken — and deliberately not
    another wait, because the condition it would wait for cannot change on its
    own.
    """
    await api_client.update_story(
        story_id,
        {
            "quarantine_reason": {
                "deploy_outcome": DeployOutcome.WAITING_INFRASTRUCTURE.value,
                "allocation_failure_reason": result.allocation_failure_reason.value,
                "allocation_required_ram_mb": result.allocation_required_ram_mb,
                "allocation_min_disk_mb": result.allocation_min_disk_mb,
                "detail": detail,
            }
        },
    )
    # Owed before the transition for the same reason the QA paths owe theirs:
    # this line takes the story out of DEPLOYING, and nothing scans it
    # afterwards. A refusal nobody is told about was previously one swallowed
    # exception away — the publish used to sit behind `except Exception: log`.
    owed = None
    if tell_owner:
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_IMPOSSIBLE_CAPACITY,
            text=IMPOSSIBLE_CAPACITY_TEXT,
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
    await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
    await _notify_admin_failure(run.id, project_id, detail)
    if owed is not None:
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
    log.warning("deploy_refusal_escalated", run_id=run.id, detail=detail, told_owner=tell_owner)


#: What the owner is told when their deploy needs an operator rather than room.
#: Deliberately not the capacity-wait message: nothing will free up that makes
#: this request fit, and the project is not at fault, so the owner is told what
#: is actually happening instead of being left to watch a wait that never ends.
IMPOSSIBLE_CAPACITY_TEXT = (
    "Deploying this project needs more capacity than any managed server can provide. "
    "Tell the user that our operators have been asked to review it, and that this is "
    "our infrastructure, not a problem with their project."
)


def _infrastructure_wait_started_at(run) -> datetime:
    """When this story started waiting for infrastructure, across re-dispatches.

    Each re-dispatch carries the stamp forward, so the bound measures how long
    the user's deploy has actually been waiting rather than restarting every time
    the wait briefly resumed and was refused again.
    """
    run_metadata = getattr(run, "run_metadata", None) or {}
    stamp = run_metadata.get(INFRASTRUCTURE_WAIT_STARTED_KEY)
    return _parse_datetime(stamp) if stamp else _parse_datetime(run.created_at)


async def _handle_deploy_infrastructure_wait(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> RefusedDeployAction:
    """A deploy that found no admissible server waits, and never fails the story.

    The wait is bounded by `supervisor.resource_wait_timeout_minutes`, the same
    bound the engineering path's wait carries: the platform may keep a user's
    work waiting on its own infrastructure only so long before somebody has to
    look. An unbounded wait is how a stuck story stays invisible.

    The bound is checked before admissibility, exactly as the engineering path
    checks the task's age before `_resources_available`, because a wait can also
    fail to end while targets keep appearing. Resuming asks whether *any* server
    could take the request, while a project already bound to a host is refused by
    *that* host: a fleet with one healthy server and one broken one the project
    sits on would otherwise re-dispatch, be refused, and re-dispatch again
    forever. Escalating on elapsed time bounds that cycle too — the same clock,
    carried across re-dispatches, ends both shapes of a wait that is not working.
    """
    waiting_since = _infrastructure_wait_started_at(run)
    waited_minutes = (datetime.now(UTC) - waiting_since).total_seconds() / 60

    if waited_minutes >= _resource_wait_timeout_minutes():
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail=(
                f"deploy waited {round(waited_minutes)} minutes for an admissible server "
                f"({result.allocation_failure_reason.value})"
            ),
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    if not await _admissible_target_exists(
        api_client,
        required_ram_mb=result.allocation_required_ram_mb,
        min_disk_mb=result.allocation_min_disk_mb,
    ):
        log.info(
            "deploy_waiting_infrastructure",
            run_id=run.id,
            waited_minutes=round(waited_minutes, 1),
        )
        return RefusedDeployAction.WAITING

    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        # No wait can supply a commit this run never recorded, so waiting for one
        # is the silent hang again. The story is not failed — a deploy run
        # without a head_sha is this platform's defect, not the project's — it
        # goes to a human.
        log.error("deploy_infrastructure_wait_head_sha_missing", run_id=run.id)
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail="deploy run has no head_sha to resume the infrastructure wait with",
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    lifecycle = await _resume_initial_owner_intent(api_client, project_id, story_id, run, head_sha)
    if lifecycle is not None:
        if lifecycle.disposition is GrantIntentLifecycleDisposition.DISPATCHED:
            log.info("infrastructure_wait_resumed_owner_intent", run_id=run.id)
            return RefusedDeployAction.REDISPATCHED
        if lifecycle.disposition is GrantIntentLifecycleDisposition.EXHAUSTED:
            await _fail_exhausted_grant_intent(api_client, story_id, project_id, run, log)
            return RefusedDeployAction.FAILED
        if lifecycle.disposition is GrantIntentLifecycleDisposition.IN_FLIGHT:
            log.info("infrastructure_wait_owner_intent_in_flight", run_id=run.id)
            return RefusedDeployAction.WAITING
        if lifecycle.disposition is GrantIntentLifecycleDisposition.STALE_TARGET:
            log.info("infrastructure_wait_owner_intent_stale_target", run_id=run.id)
            return RefusedDeployAction.WAITING

    new_run_id = f"deploy-infra-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "supervisor_infrastructure_wait",
                "head_sha": head_sha,
                INFRASTRUCTURE_WAIT_STARTED_KEY: waiting_since.isoformat(),
            },
        }
    )
    recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_after_infrastructure_wait", story_id=story_id
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=new_run_id,
            project_id=project_id,
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            story_id=story_id,
            triggered_by=DeployTrigger.WEBHOOK,
            action=DeployAction.FEATURE,
            head_sha=head_sha,
        ),
    )
    log.info("deploy_infrastructure_wait_redispatched", run_id=run.id, new_run_id=new_run_id)
    return RefusedDeployAction.REDISPATCHED


async def _handle_deploy_waiting_user_secret(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Deploy is blocked on a required user secret — park the story, ask the user once.

    The story moves DEPLOYING → WAITING_USER_SECRET (not FAILED). The request is
    emitted here, on entry to the wait, exactly once: the transition happens first,
    so the story leaves the DEPLOYING set this branch polls and cannot be asked
    again on a later tick. supervise_waiting_user_secret_stories only checks for the
    secret's arrival; it never re-sends the request.
    """
    missing = run.result.missing_user_secrets
    log.info(
        "deploy_waiting_user_secret",
        run_id=run.id,
        missing=[m.key for m in missing],
    )

    await api_client.wait_user_secret_story(story_id)

    try:
        await _request_user_secret_via_po(
            api_client, redis_client, story_id, project_id, missing, log
        )
    except Exception:
        # The story is already parked; a failed PO publish must not re-raise and
        # cause a second request next tick. It is a one-shot best-effort nudge.
        log.warning("waiting_user_secret_request_failed", story_id=story_id, exc_info=True)


async def _request_user_secret_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    missing,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask the project owner for the missing secrets, through PO, by key + description.

    Emits a POSystemEvent to po:input; PO composes the human message. The secret
    `consumers` never leave the resolver — only the key and its description reach
    the user.
    """
    recipient = await resolve_project_recipient(
        api_client, project_id, event="story_waiting_user_secret", story_id=story_id
    )
    if not recipient.is_addressable:
        log.warning("waiting_user_secret_unaddressable", project_id=project_id)
        return

    secret_lines = "\n".join(f"- {m.key}: {m.description}" for m in missing)
    text = (
        "Deployment is paused because the project needs secret(s) only the user can "
        "provide:\n"
        f"{secret_lines}\n"
        "Ask the user for each value and save it. Deployment resumes automatically "
        "once every secret is saved."
    )
    event = POSystemEvent(
        event=OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
        text=text,
        task_id=story_id,
        story_id=story_id,
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
        project_id=project_id,
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info(
        "waiting_user_secret_requested",
        story_id=story_id,
        keys=[m.key for m in missing],
    )


async def supervise_waiting_user_secret_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll WAITING_USER_SECRET stories; re-deploy once every missing secret is saved.

    Reads the missing keys from the story's latest deploy run, checks the project's
    stored secret key names, and re-dispatches the deploy — the same way RETRY does
    — when all are present, moving the story back to DEPLOYING. A story whose set is
    still incomplete stays waiting: no state change, no repeated message to the user.

    Returns dict with 'redispatched' and 'failed' counts.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.WAITING_USER_SECRET)
    if not stories:
        return {"redispatched": 0, "failed": 0}

    redispatched = 0
    failed = 0

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="deploy")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(
                api_client, story_id, project_id, "deploy", exc, log
            )
            failed += 1
            continue
        # A run without a parseable result (QUEUED/RUNNING re-dispatch already in
        # flight, or superseded) means there is nothing to act on yet — keep waiting.
        if run is None or run.result is None:
            continue

        missing_keys = [m.key for m in run.result.missing_user_secrets]
        if not missing_keys:
            continue

        present = set(await api_client.list_project_secret_keys(project_id))
        if not set(missing_keys) <= present:
            log.info(
                "waiting_user_secret_incomplete",
                missing=[k for k in missing_keys if k not in present],
            )
            continue

        redeployed = await _redispatch_waiting_deploy(
            api_client, redis_client, story_id, project_id, run, log
        )
        if redeployed:
            redispatched += 1
        else:
            failed += 1

    return {"redispatched": redispatched, "failed": failed}


async def _redispatch_waiting_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Every missing secret is saved — re-run deploy the same path RETRY uses.

    head_sha is resolved from the source run exactly as the RETRY path does; a
    missing head_sha is a typed failure (fail the story, notify admin), never a
    silent fallback to the default branch. The story is moved to DEPLOYING before
    the deploy message is created so it leaves the WAITING set; if the publish then
    fails, next tick re-derives the wait from the old run rather than wedging on a
    queued run with no message. An exhausted owner-grant intent is failed from
    WAITING_USER_SECRET instead, so this path issues exactly one Story transition.

    Returns True once re-dispatched, False if the story was failed instead.
    """
    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        log.error("waiting_user_secret_head_sha_missing", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "waiting deploy could not find original head_sha"
        )
        return False

    lifecycle = await _resume_initial_owner_intent(api_client, project_id, story_id, run, head_sha)
    disposition = lifecycle.disposition if lifecycle is not None else None
    if disposition is GrantIntentLifecycleDisposition.EXHAUSTED:
        # Failed straight out of WAITING_USER_SECRET. Moving the story to
        # DEPLOYING first and then failing it here was two Story transitions on
        # one code path, and the intermediate DEPLOYING had no owner.
        await _fail_exhausted_grant_intent(api_client, story_id, project_id, run, log)
        return False

    # The story leaves WAITING_USER_SECRET exactly once, on the paths that are
    # actually taking it further.
    await api_client.transition_story(story_id, "deploy")

    if disposition is GrantIntentLifecycleDisposition.DISPATCHED:
        log.info("waiting_secret_resumed_owner_intent", run_id=run.id)
        return True
    if disposition is GrantIntentLifecycleDisposition.IN_FLIGHT:
        log.info("waiting_secret_owner_intent_in_flight", run_id=run.id)
        return True
    if disposition is GrantIntentLifecycleDisposition.STALE_TARGET:
        log.info("waiting_secret_owner_intent_stale_target", run_id=run.id)
        return True

    new_run_id = f"deploy-secret-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "supervisor_user_secret",
                "head_sha": head_sha,
            },
        }
    )

    secret_recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_after_user_secret", story_id=story_id
    )
    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        telegram_chat_id=secret_recipient.telegram_chat_id,
        unaddressed_reason=secret_recipient.unaddressed_reason,
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
        head_sha=head_sha,
    )
    await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
    log.info("waiting_user_secret_redispatched", story_id=story_id, new_run_id=new_run_id)
    return True


# ---------------------------------------------------------------------------
# QA supervision — TESTING stories
# ---------------------------------------------------------------------------
