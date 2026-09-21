"""QA outcome, recovery, quarantine, and retry supervision."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError
import structlog

from shared.contracts.dto.qa_handoff import (
    QA_DISPATCHED_AT_KEY,
    QA_HANDOFF_KEY,
    QAHandoffPlan,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import (
    QA_HARNESS_BLOCKERS,
    QABlocker,
    QABlockerCategory,
    QAFailedCheck,
    QAFailedCheckCause,
    QARunResult,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import notify_admins_best_effort
from shared.redis import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from ..owner_notifications import (
    deliver_owed_notification,
    owe_owner_notification,
)
from .common import (
    STORY_HUMAN_REVIEW_ACTION,
    _fail_story_on_invalid_result,
    _parse_datetime,
    _qa_handoff_recovery_minutes,
)
from .handoff import _execute_qa_handoff

logger = structlog.get_logger(__name__)
MAX_QA_LOOPS = 2  # max QA→Engineering cycles before story is marked failed


def _qa_failure_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_failure_max_fingerprint_attempts")


def _qa_fix_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_max_fix_attempts")


async def supervise_testing_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll TESTING stories and route based on QA run outcome.

    Reads run.result.qa_outcome set by the QA consumer:
    - PASSED → story COMPLETED and the owner told, with the deployment's address
    - FAILED → create fix task, story IN_PROGRESS, redispatch to engineering
    - BLOCKED / EXHAUSTED / ERROR → stop the application and wait for human review

    The temporary access the run borrowed is deliberately not consulted. Handing
    the test identity back is the sweep's work and it runs on its own schedule:
    it keeps revoking, keeps reading the running service, and calls an
    administrator when it gives up. Making the product wait for that meant a
    story that deploy, smoke and QA had all passed stayed unfinished for exactly
    as long as a revoke kept being retried, and the user heard nothing. A test
    identity left behind is a cleanup incident with its own owner, not a verdict
    on the product, so it is reported there instead of held against the story.

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.TESTING)
    if not stories:
        return {"completed": 0, "redispatched": 0, "failed": 0, "recovered": 0}

    completed = 0
    redispatched = 0
    failed = 0
    recovered = 0

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        # Find latest QA run for this story
        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="qa")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(api_client, story_id, project_id, "qa", exc, log)
            failed += 1
            continue
        if run is None:
            continue

        if run.status is RunStatus.QUEUED:
            # A queued QA run in a TESTING story is either about to be picked up
            # or is the remains of a handoff that died before it finished. The
            # plan stored on the run is what tells the two apart and what lets
            # this tick finish the work the dead process started.
            if await _recover_qa_handoff(api_client, redis_client, run, log):
                recovered += 1
            continue

        if run.status is RunStatus.RUNNING:
            continue

        # A terminal QA run always carries a result (validation enforces it);
        # None here only means a superseded/non-terminal run — skip it.
        if run.result is None:
            log.info("qa_run_superseded_skip", run_id=run.id, run_status=run.status.value)
            continue

        outcome = run.result.qa_outcome

        if outcome == QAOutcome.PASSED:
            # The completion endpoint writes this record in the same transaction
            # as COMPLETED, so direct operator completion and QA completion owe
            # exactly the same durable PO instruction.
            await api_client.transition_story(story_id, "complete")
            owed = await api_client.get_story_owner_notification(story_id)
            log.info("qa_supervisor_completed", run_id=run.id)
            await deliver_owed_notification(
                api_client, redis_client, story_id, owed, log, story_record=True
            )
            completed += 1

        elif outcome == QAOutcome.FAILED:
            # Only a typed failed check is product evidence. A malformed or
            # contradictory failed result is not permission to ask engineering
            # to change customer code, so it takes the ordinary unverified QA
            # route instead.
            if run.result.blocker is not None or not run.result.failed_checks:
                await _quarantine_unverified_application(
                    api_client, redis_client, story_id, project_id, run, run.result, log
                )
                failed += 1
            elif not _product_failures(run.result):
                # Every failure lacked a QA tool or QA access: nothing about the
                # product was judged, so no fix attempt is spent on it.
                await _quarantine_unverified_application(
                    api_client,
                    redis_client,
                    story_id,
                    project_id,
                    run,
                    _with_unverifiable_checks_blocker(run.result),
                    log,
                )
                failed += 1
            else:
                dispatched = await _handle_qa_failed(
                    api_client, redis_client, story_id, project_id, run, log
                )
                if dispatched:
                    redispatched += 1
                elif dispatched is False:
                    failed += 1

        elif outcome in (QAOutcome.BLOCKED, QAOutcome.EXHAUSTED, QAOutcome.ERROR):
            await _quarantine_unverified_application(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            log.warning(
                "qa_supervisor_quarantined",
                run_id=run.id,
                outcome=outcome.value,
                application_id=run.run_metadata.get("application_id"),
            )
            failed += 1

    return {
        "completed": completed,
        "redispatched": redispatched,
        "failed": failed,
        "recovered": recovered,
    }


async def _recover_qa_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Finish a QA handoff whose process died before it did.

    Everything that decides the handoff is on the run, so this needs no memory of
    the tick that planned it. What it must not do is repeat work that landed: a
    plan that wanted access is left alone once any grant exists for the run,
    because from that moment the temporary-access sweep owns it, and a plan that
    only had to publish is left alone once the publish is stamped.

    The age bound keeps this off a handoff that is merely in progress — a run
    created seconds ago is being worked on, not abandoned. It bounds the
    publish-only plan, where the stamp is written after the publish and only the
    clock tells an unfinished handoff from a finished one whose stamp was lost.
    An access-backed plan needs no such bound: "a grant exists for this run" is
    the exact test, and a handoff the target refused left no grant at all. Making
    it wait out the recovery window is what left a second story's QA unstarted
    while the previous story's grant was already being cleaned up — the retry
    arrived minutes after the target was free.

    Returns True if this tick took the handoff over.
    """
    plan_data = run.run_metadata.get(QA_HANDOFF_KEY)
    if plan_data is None:
        # A run from before the plan was recorded, or one created by something
        # other than the deploy handoff. Nothing here can be reconstructed.
        return False
    if run.run_metadata.get(QA_DISPATCHED_AT_KEY):
        return False

    age_minutes = (datetime.now(UTC) - _parse_datetime(run.created_at)).total_seconds() / 60
    plan = QAHandoffPlan.model_validate(plan_data)
    if plan.access is not None:
        if await api_client.temporary_access_grant_exists_for_run(run.id):
            return False
    elif age_minutes < _qa_handoff_recovery_minutes():
        return False

    if plan.access is not None and age_minutes < _qa_handoff_recovery_minutes():
        # Not a dead process: the previous attempt was refused the target and
        # said so. Retrying it is the ordinary next tick of that wait.
        log.info(
            "qa_handoff_retried_without_grant",
            run_id=run.id,
            age_minutes=round(age_minutes, 1),
        )
    else:
        log.warning(
            "qa_handoff_recovered",
            run_id=run.id,
            age_minutes=round(age_minutes, 1),
            needs_access=plan.access is not None,
        )
    await _execute_qa_handoff(api_client, redis_client, run.id, plan, log)
    return True


def _qa_quarantine_reason(result: QARunResult) -> dict:
    """Keep the terminal QA evidence with the story without reclassifying it."""
    reason = {"qa_outcome": result.qa_outcome.value}
    if result.blocker is not None:
        reason["blocker"] = result.blocker.model_dump(mode="json")
    if result.summary:
        reason["summary"] = result.summary
    if result.error:
        reason["error"] = result.error
    if result.state_changes:
        reason["state_changes"] = [
            change.model_dump(mode="json") for change in result.state_changes
        ]
    if result.telegram_probe_evidence:
        reason["telegram_probe_evidence"] = [
            evidence.model_dump(mode="json") for evidence in result.telegram_probe_evidence
        ]
    return reason


async def _quarantine_unverified_application(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: QARunResult,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Stop an unverified bot, retain its binding, and request a human decision.

    ``result`` is the run's result as the supervisor classified it, which for a
    failure with no product check carries the blocker that classification typed.
    """
    application_id = run.run_metadata.get("application_id")
    if not isinstance(application_id, int):
        raise RuntimeError(f"QA run {run.id} has no application_id for quarantine")

    await api_client.stop_application(application_id)
    reason = _qa_quarantine_reason(result)
    await api_client.update_story(story_id, {"quarantine_reason": reason})
    owed = await owe_owner_notification(
        api_client,
        run,
        event=OwnerNotificationEvent.STORY_QUARANTINED,
        text=_quarantine_text(reason),
        story_id=story_id,
        project_id=project_id,
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        log=log,
    )
    await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
    await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
    harness = _harness_blocker(result)
    if harness is not None:
        # Parked once: the story has left TESTING, so no later tick reaches here
        # for this run. The operator's route back is a recheck, after the target
        # has been reconciled.
        await notify_admins_best_effort(
            f"QA harness blocker parked story {story_id} (project {project_id}) — "
            f"{harness.category.value}.\n"
            f"run: {run.id}\n"
            f"attempted: {harness.attempted}\n"
            f"sent: {harness.sent}\n"
            f"received: {harness.received}\n"
            f"No fix task was created. {_harness_repair(harness.category, story_id)}",
            level="error",
            component="supervisor",
            story_id=story_id,
            project_id=project_id,
            run_id=run.id,
        )
        log.warning("qa_supervisor_harness_blocker_parked", category=harness.category.value)


def _harness_blocker(result: QARunResult):
    """The run's blocker when it is a QA harness failure, else ``None``."""
    blocker = result.blocker
    if blocker is None or blocker.category not in QA_HARNESS_BLOCKERS:
        return None
    return blocker


def _harness_repair(category: QABlockerCategory, story_id: str) -> str:
    """What an administrator does about a parked harness blocker."""
    recheck = f"POST /api/stories/{story_id}/recheck-qa"
    if category is QABlockerCategory.QA_CHECKS_UNVERIFIABLE:
        # A recheck runs the same QA with the same tools, so it cannot close a
        # capability gap; it only helps once refused access has been repaired.
        return (
            "A qa_capability check needs a human decision on its criterion: accept it or "
            "change it, because a recheck runs the same QA tools and fails the same way. "
            f"A qa_access check needs the refused access repaired first; only then {recheck}."
        )
    return f"Repair the target (managed-target reconciliation), then {recheck}."


def _product_failures(result: QARunResult) -> list[QAFailedCheck]:
    """The failed checks that judge the product — the only ones a fix task may carry."""
    return [check for check in result.failed_checks if check.cause is QAFailedCheckCause.PRODUCT]


def _unverified_failures(result: QARunResult) -> list[QAFailedCheck]:
    """Failed checks QA had no tool or no access for; evidence, never instructions."""
    return [
        check for check in result.failed_checks if check.cause is not QAFailedCheckCause.PRODUCT
    ]


def _with_unverifiable_checks_blocker(result: QARunResult) -> QARunResult:
    """Type a failure with no product check as the blocker it is."""
    checks = _unverified_failures(result)
    blocker = QABlocker(
        category=QABlockerCategory.QA_CHECKS_UNVERIFIABLE,
        attempted="judge the product from the QA executor's failed checks",
        sent="; ".join(check.name for check in checks),
        received="; ".join(
            f"{check.cause.value}: {check.name}: {check.detail}" for check in checks
        ),
    )
    return result.model_copy(update={"blocker": blocker})


def _quarantine_text(reason: dict) -> str:
    """Ask the project owner to decide what to do with a stopped bot."""
    outcome = reason["qa_outcome"]
    blocker = reason.get("blocker")
    if blocker and QABlockerCategory(blocker["category"]) in QA_HARNESS_BLOCKERS:
        # Nothing here is about the product, so nothing asks the owner to fix it.
        return (
            "QA could not check the bot this time because of a problem in the platform's "
            "test environment, not in the product. The bot has been stopped while an "
            "administrator repairs the environment and runs the check again; its Telegram "
            "token remains assigned to this project, and nothing needs to be changed in it."
        )
    if blocker:
        detail = f"{blocker['category']}: {blocker['received']}"
    else:
        detail = reason.get("summary") or reason.get("error") or outcome
    return (
        "QA could not confirm that the bot works. The bot has been stopped, "
        f"but its Telegram token remains assigned to this project. Reason: {detail}. "
        "Please decide whether to fix and redeploy it."
    )


async def _handle_qa_failed(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool | None:
    """Create a bounded, fingerprinted fix task for a confirmed QA defect.

    Returns True if a fix task was created, False if escalation is required,
    and None when an existing task was recovered or had already been handled.
    """
    qa_run_id = run.id
    result = run.result
    summary = result.summary or "QA testing failed"
    failed_checks = _product_failures(result)
    unverified_checks = _unverified_failures(result)
    qa_summary = summary
    if unverified_checks:
        # The executor's summary speaks for every failure, including the ones
        # that are not about the product, so it cannot word or sign the fix.
        summary = "QA found product failures: " + "; ".join(c.name for c in failed_checks)

    tasks = await api_client.get_tasks_by_story(story_id)
    prior_evidence = [item for task in tasks if (item := _qa_failure_metadata(task))]
    if any(item.get("qa_run_id") == qa_run_id for item in prior_evidence):
        # create_task commits before this transition. Retry the transition when
        # a transient error left the already-created fix task behind.
        await api_client.transition_story(story_id, "start")
        log.info("qa_supervisor_failure_transition_recovered", qa_run_id=qa_run_id)
        return None

    fingerprint = _qa_failure_fingerprint(summary, failed_checks)
    matching_failures = [item for item in prior_evidence if item.get("fingerprint") == fingerprint]
    attempt = len(matching_failures) + 1
    total_attempt = len(prior_evidence) + 1
    evidence = {
        "qa_run_id": qa_run_id,
        "fingerprint": fingerprint,
        "fingerprint_attempt": attempt,
        "fix_attempt": total_attempt,
        "summary": summary,
        "failed_checks": _product_check_evidence(failed_checks),
    }
    if unverified_checks:
        evidence["qa_summary"] = qa_summary
        evidence["unverified_checks"] = [
            check.model_dump(mode="json") for check in unverified_checks
        ]

    if attempt > _qa_failure_limit() or total_attempt > _qa_fix_limit():
        exhausted_limit = _qa_failure_limit() if attempt > _qa_failure_limit() else _qa_fix_limit()
        await api_client.update_story(
            story_id,
            {"quarantine_reason": {"qa_outcome": QAOutcome.FAILED.value, "qa_failure": evidence}},
        )
        # The owner is told here, not only the administrators. This transition
        # ends the story for them exactly as a quarantine does — their product
        # stops moving until a human looks at it — and an ending they are not
        # told about is the silence this seam exists to remove. It goes through
        # the same record for the same reason: the story leaves TESTING on the
        # next line and nothing scans it afterwards.
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text=_fix_attempts_exhausted_text(summary, exhausted_limit),
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
        await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        await notify_admins_best_effort(
            f"QA failure {fingerprint} exhausted {exhausted_limit} fix attempts "
            f"for story {story_id}",
            level="warning",
            story_id=story_id,
            failure_fingerprint=fingerprint,
        )
        log.warning(
            "qa_supervisor_failure_escalated",
            fingerprint=fingerprint,
            fingerprint_attempt=attempt,
            fix_attempt=total_attempt,
            max_attempts=exhausted_limit,
        )
        return False

    issues_text = "\n".join(f"- {c.name}: {c.detail}" for c in failed_checks)
    if not issues_text:
        issues_text = summary

    fix_description = (
        f"QA testing found issues after deploy. Fix the following:\n\n"
        f"{issues_text}\n\n"
        f"QA summary: {summary}"
    )

    await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story_id,
            "title": f"QA fix: {summary[:80]}",
            "type": "fix",
            "status": TaskStatus.TODO.value,
            "description": fix_description,
            "failure_metadata": {"qa_failure": evidence},
        }
    )

    # Transition story back to IN_PROGRESS for engineering
    await api_client.transition_story(story_id, "start")

    log.info(
        "qa_supervisor_fix_task_created",
        story_id=story_id,
        fingerprint=fingerprint,
        fingerprint_attempt=attempt,
        fix_attempt=total_attempt,
    )
    return True


def _fix_attempts_exhausted_text(summary: str, exhausted_limit: int) -> str:
    """What the owner is told when QA kept failing and the fixes ran out.

    Deliberately the same event PO already routes for a quarantine: from the
    owner's side this *is* the quarantine case — the product is stopped and a
    human has to decide — and inventing a second event name would only mean PO
    dropping it as unknown.
    """
    return (
        f"QA kept finding the same problem after {exhausted_limit} attempts to fix it, "
        "so work on this story has stopped and a specialist has been asked to look at it. "
        f"The last thing QA reported: {summary}"
    )


def _qa_failure_metadata(task: object) -> dict | None:
    """Return the QA failure evidence recorded on a prior fix task."""
    metadata = getattr(task, "failure_metadata", None) or {}
    value = metadata.get("qa_failure")
    return value if isinstance(value, dict) else None


def _product_check_evidence(failed_checks: list[QAFailedCheck]) -> list[dict]:
    """Product checks as recorded before a cause existed.

    Every check here is `product`, so the cause adds nothing, and leaving it out
    keeps fingerprints and evidence identical to the fix tasks already stored.
    """
    return [check.model_dump(mode="json", exclude={"cause"}) for check in failed_checks]


def _qa_failure_fingerprint(summary: str, failed_checks: list[QAFailedCheck]) -> str:
    """Build a stable signature for a QA failure's product evidence."""
    payload = {
        "failed_checks": _product_check_evidence(failed_checks),
        "summary": summary,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
