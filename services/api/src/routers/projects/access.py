"""API-owned lifecycle for durable generated-service permanent-access intents."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    StoryFailure,
    StoryFailureCode,
    story_failure_admin_text,
    story_failure_owner_text,
)
from shared.contracts.dto.users_grant import (
    USERS_GRANT_INTENT_KEY,
    GrantIntent,
    GrantIntentDispatchTarget,
    GrantIntentExhaustion,
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleRequest,
    GrantIntentLifecycleResult,
    GrantIntentRetryCommand,
    GrantIntentStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployOutcome, DeployTrigger
from shared.models import (
    Application,
    Deployment,
    Project,
    Repository,
    Run,
    SystemConfig,
    User,
    UsersGrantIntent,
)
from shared.models.story import Story
from shared.queues import DEPLOY_QUEUE
from shared.redis.client import RedisStreamClient

from ...database import get_async_session
from ...dependencies import (
    _optional_bearer_scheme,
    get_redis_client,
    is_internal_service,
    require_internal_or_admin,
)
from ...schemas import GrantUserRequest, OwnershipTransferRequest
from .._recipients import resolve_project_recipient
from .._story_helpers import _do_transition, _land_on, _record_story_failure
from ..projects_guards import check_project_access, load_locked_project

router = APIRouter()
logger = structlog.get_logger()
DEPLOY_RETRY_CEILING_KEY = "deploy.max_deploy_retries"
_RETRY_CEILING_EXHAUSTED_DETAIL = "deployment retry ceiling exhausted"


class GrantIntentCompletion(BaseModel):
    execution_run_id: str
    active: bool
    detail: str | None = Field(default=None, max_length=512)


async def _live_target(db: AsyncSession, project_id: uuid.UUID) -> tuple[int, int, str]:
    row = (
        await db.execute(
            select(Application.id, Deployment.id, Deployment.deployed_sha)
            .join(Deployment, Deployment.application_id == Application.id)
            .join(Repository, Repository.id == Application.repo_id)
            .where(
                Repository.project_id == project_id,
                Application.status == ApplicationStatus.RUNNING.value,
                Deployment.result == DeploymentResult.SUCCESS.value,
                Deployment.deployed_sha.is_not(None),
            )
            .order_by(Deployment.deployed_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="permanent access requires a healthy deployed service with a recorded SHA",
        )
    return row[0], row[1], row[2]


async def _deployed_commit_for_deployment(db: AsyncSession, deployment_id: int) -> str:
    """The built commit a recorded deployment actually put on its target.

    A permanent-access grant redeploys the artifact that is running, so it has to
    ask for that artifact's images and tree, not for the story commit the
    deployment is keyed on. A record that predates both being written cannot be
    redeployed without guessing, and refuses instead.
    """
    info = await db.scalar(select(Deployment.deployment_info).where(Deployment.id == deployment_id))
    deployed_commit_sha = (info or {}).get("deployed_commit_sha")
    if not isinstance(deployed_commit_sha, str) or not deployed_commit_sha:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the running deployment does not name the commit it deployed",
        )
    return deployed_commit_sha


async def _verified_user(db: AsyncSession, telegram_id: int) -> User:
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="verified Telegram identity not found")
    return user


def _intent_id(kind: GrantIntentKind, project_id: uuid.UUID, telegram_id: int) -> str:
    return f"users-grant-{kind.value}-{project_id.hex}-{telegram_id}"


async def _as_dto(db: AsyncSession, project: Project, intent: UsersGrantIntent) -> GrantIntent:
    return GrantIntent(
        id=intent.id,
        kind=GrantIntentKind(intent.kind),
        project_id=str(intent.project_id),
        channel=intent.channel,
        external_id=intent.external_id,
        target_application_id=intent.target_application_id,
        target_deployment_id=intent.target_deployment_id,
        target_sha=intent.target_sha,
        target_history=intent.target_history or [],
        initiating_actor=intent.initiating_actor,
        outgoing_owner_id=intent.outgoing_owner_id,
        incoming_owner_id=intent.incoming_owner_id,
        status=GrantIntentStatus(intent.status),
        attempts=intent.attempts,
        detail=intent.detail,
        created_at=intent.created_at,
        applied_at=intent.applied_at,
        execution_run_id=intent.execution_run_id,
        retry_history=intent.retry_history or [],
        exhaustion=await _exhaustion(db, project, intent),
    )


def _target_changed(intent: UsersGrantIntent, target: tuple[int | None, int | None, str]) -> bool:
    return (intent.target_application_id, intent.target_deployment_id, intent.target_sha) != target


def _target_was_superseded(
    intent: UsersGrantIntent, target: tuple[int | None, int | None, str]
) -> bool:
    """Return whether an automatic source Run names a prior immutable target."""
    return any(entry.get("sha") == target[2] for entry in intent.target_history or [])


def _is_exhausted(intent: UsersGrantIntent) -> bool:
    return (
        intent.status == GrantIntentStatus.FAILED.value
        and intent.detail == _RETRY_CEILING_EXHAUSTED_DETAIL
    )


async def _exhaustion(
    db: AsyncSession,
    project: Project,
    intent: UsersGrantIntent,
    *,
    for_stop: bool = False,
) -> GrantIntentExhaustion | None:
    """One typed decision for the current exhausted epoch and its retry fence."""
    if intent.kind != GrantIntentKind.INITIAL_OWNER.value or not _is_exhausted(intent):
        return None
    evidence = (
        await _current_source_story(db, project, intent)
        if intent.attempts > 0 and intent.execution_run_id is not None
        else None
    )
    command = (
        GrantIntentRetryCommand(expected_execution_run_id=evidence[0].id)
        if evidence is not None and project.status != ProjectStatus.ARCHIVED.value
        else None
    )
    decision = GrantIntentExhaustion(
        attempts=intent.attempts,
        target=GrantIntentDispatchTarget(
            application_id=intent.target_application_id,
            deployment_id=intent.target_deployment_id,
            sha=intent.target_sha,
        ),
        exhausted_execution_run_id=intent.execution_run_id,
        action="retry_initial_owner_deployment" if command is not None else None,
        retry_command=command,
    )
    if command is not None:
        story = evidence[1]
        if story.status == StoryStatus.FAILED.value:
            released_stop = (
                story.quarantine_reason is None
                and story.status_entered_at is not None
                and story.status_entered_at >= evidence[0].created_at
            )
            if not (released_stop or _matching_exhaustion_stop(story, intent, decision)):
                command = None
        elif (
            not for_stop
            or story.status
            not in {
                StoryStatus.PR_REVIEW.value,
                StoryStatus.DEPLOYING.value,
                StoryStatus.WAITING_USER_SECRET.value,
            }
            or story.quarantine_reason is not None
        ):
            command = None
    if command is not None and await _deploy_retry_ceiling(db) == 0:
        command = None
    if command is None and decision.action is not None:
        return decision.model_copy(update={"action": None, "retry_command": None})
    return decision


def _exhaustion_failure(intent: UsersGrantIntent, decision: GrantIntentExhaustion) -> StoryFailure:
    if decision.attempts > 0:
        detail = (
            f"Intent {intent.id}; exhausted attempt {intent.execution_run_id}; "
            f"target {intent.target_sha}; {intent.attempts} attempts. "
            "Check the authenticated current initial-owner deployment readback "
            "for available actions."
        )
    else:
        detail = (
            f"Intent {intent.id}; target {intent.target_sha}; no deployment Run was admitted. "
            "Same-target retry is unavailable for this exhausted target. "
            "Check the authenticated current initial-owner deployment readback "
            "for available actions."
        )
    return StoryFailure(
        code=StoryFailureCode.INITIAL_OWNER_DEPLOYMENT_EXHAUSTED,
        source="api",
        detail=detail,
    )


def _source_matches(
    project: Project,
    intent: UsersGrantIntent,
    run: Run,
    story: Story,
    *,
    allow_owed: bool = False,
) -> bool:
    """Native current binding, including released Runs predating typed stops."""
    metadata = run.run_metadata
    timeline = story.generated_product_timeline
    if not isinstance(metadata, dict) or not isinstance(timeline, dict):
        return False
    pr = timeline.get("pull_request")
    if not isinstance(pr, dict):
        return False
    cycle = story.reopened_at or story.created_at
    try:
        cancelled_with_result = (
            run.status == RunStatus.CANCELLED.value
            and run.result is not None
            and DeployRunResult.model_validate(run.result).deploy_outcome is DeployOutcome.CANCELLED
        )
        merged_at = datetime.fromisoformat(pr["merged_at"])
        built = GrantIntentDispatchTarget(sha=metadata["deployed_commit_sha"]).sha
        matches = (
            cycle <= merged_at <= run.created_at <= datetime.now(UTC)
            and run.project_id == project.id == story.project_id == intent.project_id
            and run.user_id == project.owner_id
            and run.id == intent.execution_run_id
            and run.type == RunType.DEPLOY.value
            and (
                run.status in {RunStatus.FAILED.value, RunStatus.COMPLETED.value}
                or cancelled_with_result
                or (allow_owed and run.status == RunStatus.QUEUED.value and run.result is None)
            )
            and run.story_id == story.id
            and metadata.get(USERS_GRANT_INTENT_KEY) == intent.id
            and metadata.get("head_sha") == intent.target_sha == pr.get("head_sha")
            and built == pr.get("merge_commit_sha")
            and pr.get("state") == "closed"
            and type(story.pr_number) is int
            and story.pr_number == pr.get("number")
            and intent.target_application_id is None
            and intent.target_deployment_id is None
        )
        if "grant_story_cycle" in metadata:
            matches = matches and metadata["grant_story_cycle"] == cycle.isoformat()
        if "grant_pr_number" in metadata:
            matches = matches and metadata["grant_pr_number"] == story.pr_number
        return matches
    except (KeyError, TypeError, ValueError, ValidationError):
        return False


async def _current_source_story(
    db: AsyncSession, project: Project, intent: UsersGrantIntent, *, allow_owed: bool = False
) -> tuple[Run, Story] | None:
    owner = await db.get(User, project.owner_id)
    if (
        owner is None
        or owner.telegram_id is None
        or intent.id != _intent_id(GrantIntentKind.INITIAL_OWNER, project.id, owner.telegram_id)
        or intent.channel != "telegram"
        or intent.external_id != str(owner.telegram_id)
    ):
        return None
    source = await db.get(Run, intent.execution_run_id) if intent.execution_run_id else None
    if source is None or source.story_id is None:
        return None
    story = await db.scalar(select(Story).where(Story.id == source.story_id).with_for_update())
    source = await db.scalar(select(Run).where(Run.id == source.id).with_for_update())
    if (
        story is None
        or source is None
        or not _source_matches(project, intent, source, story, allow_owed=allow_owed)
    ):
        return None
    if allow_owed and (
        source.status != RunStatus.QUEUED.value
        or story.status != StoryStatus.DEPLOYING.value
        or story.quarantine_reason is not None
        or project.status == ProjectStatus.ARCHIVED.value
    ):
        return None
    candidates = (
        await db.scalars(
            select(Run).where(Run.project_id == project.id, Run.type == RunType.DEPLOY.value)
        )
    ).all()
    if not _epoch_matches(intent, source, candidates):
        return None
    other_live = await db.scalar(
        select(Run.id)
        .where(
            Run.project_id == project.id,
            Run.id != source.id,
            Run.status.not_in(
                [RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value]
            ),
        )
        .limit(1)
    )
    return None if other_live is not None else (source, story)


def _epoch_matches(intent: UsersGrantIntent, source: Run, candidates: list[Run]) -> bool:
    """An exhaustion label alone is insufficient: count real native admissions."""
    epoch = len(intent.retry_history or []) + len(intent.target_history or [])
    marked = "grant_epoch" in source.run_metadata
    if marked and (
        type(source.run_metadata["grant_epoch"]) is not int
        or source.run_metadata["grant_epoch"] != epoch
        or source.run_metadata.get("grant_attempt") != intent.attempts
    ):
        return False
    if not marked and intent.retry_history:
        return False  # INITIAL_OWNER retries have always carried these epoch facts
    admissions = [
        run
        for run in candidates
        if (
            isinstance(run.run_metadata, dict)
            and run.run_metadata.get(USERS_GRANT_INTENT_KEY) == intent.id
            and run.run_metadata.get("head_sha") == intent.target_sha
            and (
                run.run_metadata.get("grant_epoch") == epoch
                if marked
                else run.created_at <= source.created_at
            )
        )
    ]
    if len(admissions) != intent.attempts or source.id not in {run.id for run in admissions}:
        return False
    if marked:
        ordinals = [run.run_metadata.get("grant_attempt") for run in admissions]
        return all(type(n) is int for n in ordinals) and sorted(ordinals) == list(
            range(1, intent.attempts + 1)
        )
    return True


def _matching_exhaustion_stop(
    story: Story, intent: UsersGrantIntent, decision: GrantIntentExhaustion
) -> bool:
    reason = story.quarantine_reason or {}
    notice = story.owner_notification
    failure = _exhaustion_failure(intent, decision)
    return (
        story.status == StoryStatus.FAILED.value
        and isinstance(reason, dict)
        and reason.get("code") == failure.code.value
        and reason.get("source") == failure.source
        and reason.get("detail") == failure.detail
        and isinstance(notice, dict)
        and notice.get("story_id") == story.id
        and notice.get("project_id") == str(story.project_id)
        and notice.get("terminal_status") == StoryStatus.FAILED.value
        and notice.get("text") == story_failure_owner_text(failure)
        and notice.get("admin_text")
        == story_failure_admin_text(story.id, str(story.project_id), failure)
    )


async def _stop_exhausted_initial_owner(
    db: AsyncSession,
    project: Project,
    intent: UsersGrantIntent,
    story_id: str | None,
    head_sha: str,
    built_sha: str,
    merged_pr_number: int | None = None,
) -> None:
    if intent.attempts == 0 and intent.execution_run_id is None:
        if merged_pr_number is None or project.status == ProjectStatus.ARCHIVED.value:
            return
        story = await db.scalar(select(Story).where(Story.id == story_id).with_for_update())
        if (
            story is None
            or story.project_id != project.id
            or story.status != StoryStatus.PR_REVIEW.value
            or story.quarantine_reason is not None
            or intent.target_sha != head_sha
            or intent.target_application_id is not None
            or intent.target_deployment_id is not None
        ):
            return
        other_live = await db.scalar(
            select(Run.id)
            .where(
                Run.project_id == project.id,
                Run.status.not_in(
                    [RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value]
                ),
            )
            .limit(1)
        )
        if other_live is not None:
            return
        await _require_current_merged_target(
            db, project, story_id, merged_pr_number, head_sha, built_sha
        )
        decision = await _exhaustion(db, project, intent, for_stop=True)
        assert decision is not None and decision.retry_command is None
        _record_story_failure(story, _exhaustion_failure(intent, decision), StoryStatus.FAILED)
        _do_transition(story, StoryStatus.FAILED)
        return
    evidence = await _current_source_story(db, project, intent)
    if evidence is None:
        return
    source, story = evidence
    if (story.id, intent.target_sha, source.run_metadata["deployed_commit_sha"]) != (
        story_id,
        head_sha,
        built_sha,
    ):
        return
    decision = await _exhaustion(db, project, intent, for_stop=True)
    assert decision is not None
    if _matching_exhaustion_stop(story, intent, decision):
        return  # response loss/replay retains the exact notice episode
    if story.quarantine_reason is not None or story.status not in {
        StoryStatus.PR_REVIEW.value,
        StoryStatus.DEPLOYING.value,
        StoryStatus.WAITING_USER_SECRET.value,
    }:
        return
    _record_story_failure(story, _exhaustion_failure(intent, decision), StoryStatus.FAILED)
    _do_transition(story, StoryStatus.FAILED)


async def _retry_story_for_epoch(
    db: AsyncSession,
    project: Project,
    intent: UsersGrantIntent,
    story_id: str | None,
    built_sha: str,
) -> Story:
    evidence = await _current_source_story(db, project, intent)
    if evidence is None:
        raise HTTPException(status_code=409, detail="retry lacks current immutable deploy evidence")
    source, story = evidence
    if source.story_id != story_id or source.run_metadata["deployed_commit_sha"] != built_sha:
        raise HTTPException(status_code=409, detail="retry source target changed")
    decision = await _exhaustion(db, project, intent)
    if decision is None or decision.retry_command is None:
        raise HTTPException(status_code=409, detail="exhausted target has no current retry command")
    # Released bare FAILED stops require the genuine current intent/Run/PR
    # binding and a landing after that Run. Text alone grants no recovery.
    released_stop = (
        story.status == StoryStatus.FAILED.value
        and story.quarantine_reason is None
        and story.status_entered_at is not None
        and story.status_entered_at >= source.created_at
    )
    if not (_matching_exhaustion_stop(story, intent, decision) or released_stop):
        raise HTTPException(status_code=409, detail="Story does not carry this deployment stop")
    return story


async def _execution_is_live(db: AsyncSession, intent: UsersGrantIntent) -> Run | None:
    if not intent.execution_run_id:
        return None
    run = await db.get(Run, intent.execution_run_id)
    if run is None or run.status in {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }:
        return None
    return run


async def _deploy_retry_ceiling(db: AsyncSession) -> int:
    """Read and lock the scheduler's retry ceiling for lifecycle admission."""
    config = await db.scalar(
        select(SystemConfig).where(SystemConfig.key == DEPLOY_RETRY_CEILING_KEY).with_for_update()
    )
    if config is None:
        raise RuntimeError(f"Missing required system config: {DEPLOY_RETRY_CEILING_KEY}")
    try:
        ceiling = int(config.value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{DEPLOY_RETRY_CEILING_KEY} must be an integer") from exc
    if ceiling < 0:
        raise RuntimeError(f"{DEPLOY_RETRY_CEILING_KEY} must be non-negative")
    return ceiling


async def _require_current_merged_target(
    db: AsyncSession,
    project: Project,
    story_id: str | None,
    pr_number: int,
    head_sha: str,
    built_sha: str,
) -> None:
    """Authorize replacement from the poller's persisted App/CI reading, under lock."""
    story = await db.scalar(select(Story).where(Story.id == story_id).with_for_update())
    if story is None or story.project_id != project.id:
        raise HTTPException(
            status_code=409, detail="merged repair story does not belong to project"
        )
    timeline = story.generated_product_timeline or {}
    if not isinstance(timeline, dict):
        raise HTTPException(
            status_code=409, detail="merged repair publication evidence is malformed"
        )
    observation = timeline.get("deploy_observation") or {}
    pr = timeline.get("pull_request") or {}
    ci = timeline.get("latest_ci_observation") or {}
    runs = timeline.get("ci_runs", [])
    if (
        not all(isinstance(value, dict) for value in (observation, pr, ci))
        or not isinstance(runs, list)
        or not all(isinstance(run, dict) for run in runs)
    ):
        raise HTTPException(
            status_code=409, detail="merged repair publication evidence is malformed"
        )
    repo = await db.scalar(
        select(Repository).where(Repository.project_id == project.id, Repository.role == "primary")
    )
    matches = (
        story.status in {StoryStatus.PR_REVIEW.value, StoryStatus.DEPLOYING.value}
        and story.pr_number == pr_number == pr.get("number")
        and pr.get("state") == "closed"
        and pr.get("head_sha") == head_sha
        and pr.get("merge_commit_sha") == built_sha
        and observation.get("story_id") == story.id
        and observation.get("project_id") == str(project.id)
        and repo is not None
        and observation.get("repository_url") == repo.git_url
        and ci.get("ci_status") == "completed"
        and ci.get("ci_conclusion") == "success"
        and type(ci.get("ci_run_id")) is int
        and ci["ci_run_id"] > 0
    )
    matching_runs = [run for run in runs if run.get("id") == ci.get("ci_run_id")]
    run = matching_runs[0] if len(matching_runs) == 1 else {}
    matches = (
        matches
        and run.get("branch") == "main"
        and run.get("head_sha") == built_sha
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )
    try:
        merged_at = datetime.fromisoformat(pr["merged_at"])
        observed_at = datetime.fromisoformat(observation["observed_at"])
        cycle_start = story.reopened_at or story.created_at
        matches = matches and cycle_start <= merged_at <= observed_at <= datetime.now(UTC)
    except (KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise HTTPException(
            status_code=409,
            detail="merged repair lacks matching current-cycle PR/publication evidence",
        )


async def _lifecycle(  # noqa: PLR0913, C901
    db: AsyncSession,
    project: Project,
    *,
    target_user: User,
    kind: GrantIntentKind,
    actor: str,
    target: tuple[int | None, int | None, str],
    deployed_commit_sha: str,
    story_id: str | None,
    explicit_user_retry: bool = False,
    merged_pr_number: int | None = None,
    retry_command: GrantIntentRetryCommand | None = None,
    expected_execution_run_id: str | None = None,
) -> tuple[UsersGrantIntent, Run | None, bool, GrantIntentLifecycleDisposition]:
    """The sole create/lookup/rebind/dispatch-preparation operation.

    The durable record is locked while this decides whether to resume its one
    live execution or create a fresh Run. Rebinding records the previous target
    and never changes a prior attempt's SHA, story, or audit identity.
    """
    intent_id = _intent_id(kind, project.id, target_user.telegram_id)
    intent = (
        await db.execute(
            select(UsersGrantIntent).where(UsersGrantIntent.id == intent_id).with_for_update()
        )
    ).scalar_one_or_none()
    created = intent is None
    if expected_execution_run_id is not None and intent is None:
        raise HTTPException(status_code=409, detail="owed execution is no longer current")
    if intent is None:
        intent = UsersGrantIntent(
            id=intent_id,
            kind=kind.value,
            project_id=project.id,
            channel="telegram",
            external_id=str(target_user.telegram_id),
            target_application_id=target[0],
            target_deployment_id=target[1],
            target_sha=target[2],
            target_history=[],
            initiating_actor=actor,
            outgoing_owner_id=project.owner_id if kind is GrantIntentKind.INCOMING_OWNER else None,
            incoming_owner_id=target_user.id if kind is GrantIntentKind.INCOMING_OWNER else None,
            status=GrantIntentStatus.PUBLISH_OWED.value,
            retry_history=[],
        )
        db.add(intent)
        await db.flush()
    elif intent.status == GrantIntentStatus.APPLIED.value:
        return intent, None, False, GrantIntentLifecycleDisposition.ALREADY_APPLIED
    target_changed = _target_changed(intent, target)
    if expected_execution_run_id is not None:
        evidence = (
            await _current_source_story(db, project, intent, allow_owed=True)
            if intent.status == GrantIntentStatus.PUBLISH_OWED.value
            and intent.execution_run_id == expected_execution_run_id
            and not target_changed
            else None
        )
        if (
            evidence is None
            or evidence[0].id != expected_execution_run_id
            or evidence[0].story_id != story_id
            or evidence[0].run_metadata["deployed_commit_sha"] != deployed_commit_sha
        ):
            raise HTTPException(status_code=409, detail="owed execution is no longer current")
    if not explicit_user_retry and target_changed:
        # Automatic lifecycle callers carry source-Run metadata. It must never
        # replace a current execution or revive a target this intent has
        # already superseded. These checks are in the admission transaction,
        # before a replacement Run can be committed or published.
        live_run = await _execution_is_live(db, intent)
        if live_run is not None:
            return intent, live_run, False, GrantIntentLifecycleDisposition.IN_FLIGHT
        if _is_exhausted(intent) and merged_pr_number is None:
            return intent, None, False, GrantIntentLifecycleDisposition.EXHAUSTED
        if _target_was_superseded(intent, target):
            return intent, None, False, GrantIntentLifecycleDisposition.STALE_TARGET
        if merged_pr_number is None:
            return intent, None, False, GrantIntentLifecycleDisposition.STALE_TARGET
        await _require_current_merged_target(
            db, project, story_id, merged_pr_number, target[2], deployed_commit_sha
        )

    rebound = False
    if target_changed:
        intent.target_history = [
            *(intent.target_history or []),
            {
                "application_id": intent.target_application_id,
                "deployment_id": intent.target_deployment_id,
                "sha": intent.target_sha,
                "attempts": intent.attempts,
                "replaced_at": datetime.now(UTC).isoformat(),
            },
        ]
        intent.target_application_id, intent.target_deployment_id, intent.target_sha = target
        intent.status = GrantIntentStatus.PUBLISH_OWED.value
        intent.detail = None
        # A deployment binding is an execution context. Its admission counter
        # cannot exhaust the replacement target before it receives a Run.
        intent.attempts = 0
        rebound = True

    live_run = None if rebound else await _execution_is_live(db, intent)
    if live_run is not None:
        return intent, live_run, False, GrantIntentLifecycleDisposition.IN_FLIGHT

    ceiling = await _deploy_retry_ceiling(db)
    retry_story = None
    if retry_command is not None:
        if (
            kind is not GrantIntentKind.INITIAL_OWNER
            or target_changed
            or retry_command.expected_execution_run_id != intent.execution_run_id
            or any(
                entry.get("expected_execution_run_id") == retry_command.expected_execution_run_id
                for entry in intent.retry_history or []
            )
        ):
            return intent, None, False, GrantIntentLifecycleDisposition.STALE_TARGET
        if not _is_exhausted(intent):
            raise HTTPException(status_code=409, detail="initial-owner deployment is not exhausted")
        if ceiling == 0:
            return intent, None, False, GrantIntentLifecycleDisposition.EXHAUSTED
        retry_story = await _retry_story_for_epoch(
            db, project, intent, story_id, deployed_commit_sha
        )
    if (
        (
            explicit_user_retry
            and kind in {GrantIntentKind.ADD_USER, GrantIntentKind.INCOMING_OWNER}
            or retry_command is not None
        )
        and _is_exhausted(intent)
        and (retry_command is not None or intent.attempts >= ceiling)
        and ceiling > 0
    ):
        # Only a new explicit user request can open a same-target retry epoch.
        # Automatic recovery has no path to this flag, so a failed intent stays
        # terminal to supervisor, PR-poller, infrastructure, and secret retries.
        intent.retry_history = [
            *(intent.retry_history or []),
            {
                "application_id": intent.target_application_id,
                "deployment_id": intent.target_deployment_id,
                "sha": intent.target_sha,
                "attempts": intent.attempts,
                "reason": "explicit_retry",
                "actor": actor,
                **(
                    {
                        "expected_execution_run_id": retry_command.expected_execution_run_id,
                        "story_id": retry_story.id,
                        "story_cycle": (
                            retry_story.reopened_at or retry_story.created_at
                        ).isoformat(),
                        "story_stop": retry_story.quarantine_reason,
                        "owner_notification": retry_story.owner_notification,
                    }
                    if retry_command is not None and retry_story is not None
                    else {}
                ),
                "restarted_at": datetime.now(UTC).isoformat(),
            },
        ]
        intent.attempts = 0
        intent.status = GrantIntentStatus.PUBLISH_OWED.value
        intent.detail = None
        if retry_story is not None:
            _land_on(retry_story, StoryStatus.DEPLOYING)
            retry_story.quarantine_reason = None

    if (
        kind is GrantIntentKind.INITIAL_OWNER and _is_exhausted(intent)
    ) or intent.attempts >= ceiling:
        intent.status = GrantIntentStatus.FAILED.value
        intent.detail = _RETRY_CEILING_EXHAUSTED_DETAIL
        if kind is GrantIntentKind.INITIAL_OWNER:
            await _stop_exhausted_initial_owner(
                db, project, intent, story_id, target[2], deployed_commit_sha, merged_pr_number
            )
        return intent, None, created, GrantIntentLifecycleDisposition.EXHAUSTED

    story = (
        await db.scalar(select(Story).where(Story.id == story_id).with_for_update())
        if story_id
        else None
    )
    run = Run(
        id=f"deploy-grant-{uuid.uuid4().hex}",
        type=RunType.DEPLOY.value,
        project_id=project.id,
        user_id=project.owner_id,
        story_id=story_id,
        status=RunStatus.QUEUED.value,
        run_metadata={
            "head_sha": target[2],
            "deployed_commit_sha": deployed_commit_sha,
            "triggered_by": "users_grant_intent",
            "deploy_action": (
                DeployAction.CREATE.value if target[0] is None else DeployAction.FEATURE.value
            ),
            USERS_GRANT_INTENT_KEY: intent.id,
            "grant_attempt": intent.attempts + 1,
            "grant_epoch": len(intent.retry_history or []) + len(intent.target_history or []),
            **(
                {
                    "grant_story_cycle": (story.reopened_at or story.created_at).isoformat(),
                    "grant_pr_number": story.pr_number,
                }
                if story is not None
                else {}
            ),
        },
    )
    db.add(run)
    await db.flush()
    intent.execution_run_id = run.id
    intent.status = GrantIntentStatus.PUBLISH_OWED.value
    intent.attempts += 1
    return intent, run, created, GrantIntentLifecycleDisposition.DISPATCHED


async def _dispatch_lifecycle(
    db: AsyncSession,
    redis: RedisStreamClient,
    project: Project,
    intent: UsersGrantIntent,
    run: Run | None,
    disposition: GrantIntentLifecycleDisposition,
    created: bool,
) -> GrantIntentLifecycleResult:
    # _lifecycle is the one admission transaction. Commit a terminal or
    # in-flight result before returning, and commit every new Run before its
    # publish. This keeps each reported durable state re-readable.
    exhaustion = await _exhaustion(db, project, intent)
    if run is None:
        await db.commit()
    if run is not None and intent.status == GrantIntentStatus.PUBLISH_OWED.value:
        # A Run this lifecycle is resuming, rather than one it just created, can
        # be older than the field. Refuse it by name instead of raising a
        # KeyError: the deploy would otherwise have to guess which commit to
        # deploy, and that guess is the defect this whole change removes.
        deployed_commit_sha = run.run_metadata.get("deployed_commit_sha")
        if not deployed_commit_sha:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this grant's deploy run does not name the commit it deploys",
            )
        recipient = await resolve_project_recipient(db, project.id, event="users_grant_intent")
        message = DeployMessage(
            task_id=run.id,
            project_id=str(project.id),
            # The consumer seeds the confirmed brief's settings through this
            # story; a live-target grant has none and keeps the DTO's "".
            story_id=run.story_id or "",
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            triggered_by=DeployTrigger.PO,
            action=DeployAction(run.run_metadata["deploy_action"]),
            head_sha=intent.target_sha,
            deployed_commit_sha=deployed_commit_sha,
        )
        await db.commit()
        # The Run must exist before publication. Reacquire the same project and
        # intent locks across the owed publish so a concurrent admission cannot
        # also publish the live Run in this commit-to-queue interval.
        await load_locked_project(db, project.id)
        await db.refresh(intent, with_for_update=True)
        if intent.execution_run_id != run.id:
            raise HTTPException(status_code=409, detail="grant dispatch was superseded")
        if intent.status == GrantIntentStatus.PUBLISH_OWED.value:
            try:
                await redis.publish_message(DEPLOY_QUEUE, message)
            except Exception:
                raise HTTPException(
                    status_code=503, detail="grant intent is durable but dispatch is still owed"
                ) from None
            intent.status = GrantIntentStatus.QUEUED.value
        await db.commit()
    if disposition is GrantIntentLifecycleDisposition.DISPATCHED:
        assert run is not None
        return GrantIntentLifecycleResult(
            intent_id=intent.id,
            status=GrantIntentStatus(intent.status),
            disposition=disposition,
            execution_run_id=run.id,
            target=GrantIntentDispatchTarget(
                application_id=intent.target_application_id,
                deployment_id=intent.target_deployment_id,
                sha=intent.target_sha,
            ),
            created=created,
        )
    return GrantIntentLifecycleResult(
        intent_id=intent.id,
        status=GrantIntentStatus(intent.status),
        disposition=disposition,
        created=created,
        exhaustion=exhaustion,
    )


async def _stage_live_intent(  # noqa: PLR0913
    db: AsyncSession,
    redis: RedisStreamClient,
    project: Project,
    target_user: User,
    kind: GrantIntentKind,
    actor: str,
) -> GrantIntentLifecycleResult:
    target = await _live_target(db, project.id)
    intent, run, created, disposition = await _lifecycle(
        db,
        project,
        target_user=target_user,
        kind=kind,
        actor=actor,
        target=target,
        deployed_commit_sha=await _deployed_commit_for_deployment(db, target[1]),
        story_id=None,
        explicit_user_retry=True,
    )
    return await _dispatch_lifecycle(db, redis, project, intent, run, disposition, created)


@router.post("/{project_id}/users/grant")
async def grant_user(
    project_id: uuid.UUID,
    body: GrantUserRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> GrantIntentLifecycleResult:
    project = await load_locked_project(db, project_id)
    actor = await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    lifecycle = await _stage_live_intent(
        db,
        redis,
        project,
        await _verified_user(db, body.telegram_id),
        GrantIntentKind.ADD_USER,
        f"user:{actor.id}" if actor is not None else "internal_service",
    )
    logger.info(
        "users_grant_intent_staged", intent_id=lifecycle.intent_id, created=lifecycle.created
    )
    return lifecycle


@router.post("/{project_id}/ownership-transfer")
async def transfer_ownership(
    project_id: uuid.UUID,
    body: OwnershipTransferRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> GrantIntentLifecycleResult:
    project = await load_locked_project(db, project_id)
    actor = await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    lifecycle = await _stage_live_intent(
        db,
        redis,
        project,
        await _verified_user(db, body.telegram_id),
        GrantIntentKind.INCOMING_OWNER,
        f"user:{actor.id}" if actor is not None else "internal_service",
    )
    return lifecycle


@router.post("/{project_id}/users/grant-intents/lifecycle")
async def resume_initial_owner_intent(
    project_id: uuid.UUID,
    body: GrantIntentLifecycleRequest,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _internal: None = Depends(require_internal_or_admin),
) -> GrantIntentLifecycleResult:
    """Internal seed/recovery entrypoint; producers cannot attach grants themselves."""
    if body.kind is not GrantIntentKind.INITIAL_OWNER or body.head_sha is None:
        raise HTTPException(
            status_code=422, detail="only initial-owner lifecycle requires an exact SHA"
        )
    if body.deployed_commit_sha is None:
        raise HTTPException(
            status_code=422,
            detail="initial-owner lifecycle must name the built commit its deploy deploys",
        )
    project = await load_locked_project(db, project_id)
    if "tg_bot" not in (project.config or {}).get("modules", []):
        raise HTTPException(
            status_code=409, detail="initial owner grant requires a Telegram service"
        )
    owner = await db.get(User, project.owner_id)
    if owner is None or owner.telegram_id is None:
        raise HTTPException(
            status_code=409, detail="project owner has no verified Telegram identity"
        )
    intent, run, created, disposition = await _lifecycle(
        db,
        project,
        target_user=owner,
        kind=body.kind,
        actor="deploy_lifecycle",
        target=(None, None, body.head_sha),
        deployed_commit_sha=body.deployed_commit_sha,
        story_id=body.story_id,
        merged_pr_number=body.merged_pr_number,
        expected_execution_run_id=body.expected_execution_run_id,
    )
    return await _dispatch_lifecycle(db, redis, project, intent, run, disposition, created)


async def _initial_owner(db: AsyncSession, project: Project) -> User:
    if project.status == ProjectStatus.ARCHIVED.value:
        raise HTTPException(status_code=409, detail="archived project cannot retry deployment")
    owner = await db.get(User, project.owner_id)
    if (
        owner is None
        or owner.telegram_id is None
        or "tg_bot" not in (project.config or {}).get("modules", [])
    ):
        raise HTTPException(
            status_code=409, detail="project has no verified initial Telegram owner"
        )
    return owner


@router.get("/{project_id}/users/initial-owner-deployment")
async def get_initial_owner_deployment(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> GrantIntent:
    project = await load_locked_project(db, project_id)
    await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    owner = await _initial_owner(db, project)
    intent = await db.get(
        UsersGrantIntent, _intent_id(GrantIntentKind.INITIAL_OWNER, project.id, owner.telegram_id)
    )
    if intent is None:
        raise HTTPException(status_code=404, detail="initial-owner intent not found")
    return await _as_dto(db, project, intent)


@router.post("/{project_id}/users/grant-intents/{intent_id}/retry")
async def retry_initial_owner_deployment(
    project_id: uuid.UUID,
    intent_id: str,
    body: GrantIntentRetryCommand,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> GrantIntentLifecycleResult:
    project = await load_locked_project(db, project_id)
    actor = await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    if actor is None:
        raise HTTPException(
            status_code=403,
            detail="deliberate retry requires an authenticated owner or administrator",
        )
    owner = await _initial_owner(db, project)
    intent = await db.scalar(
        select(UsersGrantIntent).where(UsersGrantIntent.id == intent_id).with_for_update()
    )
    if intent is None or intent.project_id != project.id:
        raise HTTPException(status_code=404, detail="grant intent not found")
    if (
        intent.id != _intent_id(GrantIntentKind.INITIAL_OWNER, project.id, owner.telegram_id)
        or intent.kind != GrantIntentKind.INITIAL_OWNER.value
        or intent.channel != "telegram"
        or intent.external_id != str(owner.telegram_id)
    ):
        raise HTTPException(
            status_code=409, detail="intent is not for the current verified project owner"
        )
    # Replays retain per-call truth, including owed dispatch recovery. Never
    # refresh a caller's exhausted-attempt fence from a later durable epoch.
    if intent.status == GrantIntentStatus.APPLIED.value:
        return await _dispatch_lifecycle(
            db, redis, project, intent, None, GrantIntentLifecycleDisposition.ALREADY_APPLIED, False
        )
    live = await _execution_is_live(db, intent)
    if live is not None:
        return await _dispatch_lifecycle(
            db, redis, project, intent, live, GrantIntentLifecycleDisposition.IN_FLIGHT, False
        )
    if body.expected_execution_run_id != intent.execution_run_id:
        return await _dispatch_lifecycle(
            db, redis, project, intent, None, GrantIntentLifecycleDisposition.STALE_TARGET, False
        )
    source = await db.get(Run, intent.execution_run_id) if intent.execution_run_id else None
    if (
        source is None
        or not isinstance(source.run_metadata, dict)
        or "deployed_commit_sha" not in source.run_metadata
    ):
        raise HTTPException(
            status_code=409, detail="exhausted intent lacks an immutable built commit"
        )
    intent, run, created, disposition = await _lifecycle(
        db,
        project,
        target_user=owner,
        kind=GrantIntentKind.INITIAL_OWNER,
        actor=f"user:{actor.id}",
        target=(intent.target_application_id, intent.target_deployment_id, intent.target_sha),
        deployed_commit_sha=source.run_metadata["deployed_commit_sha"],
        story_id=source.story_id,
        retry_command=body,
    )
    return await _dispatch_lifecycle(db, redis, project, intent, run, disposition, created)


@router.get("/{project_id}/users/grant-intents/{intent_id}")
async def get_intent(
    project_id: uuid.UUID,
    intent_id: str,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> GrantIntent:
    project = await load_locked_project(db, project_id)
    await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    intent = await db.get(UsersGrantIntent, intent_id)
    if intent is None or intent.project_id != project_id:
        raise HTTPException(status_code=404, detail="grant intent not found")
    return await _as_dto(db, project, intent)


@router.post("/{project_id}/users/grant-intents/{intent_id}/complete")
async def complete_intent(
    project_id: uuid.UUID,
    intent_id: str,
    body: GrantIntentCompletion,
    db: AsyncSession = Depends(get_async_session),
    _internal: None = Depends(require_internal_or_admin),
) -> dict:
    """Persist worker readback. APPLIED wins redelivery and cannot regress."""
    intent = (
        await db.execute(
            select(UsersGrantIntent).where(UsersGrantIntent.id == intent_id).with_for_update()
        )
    ).scalar_one_or_none()
    if intent is None or intent.project_id != project_id:
        raise HTTPException(status_code=404, detail="grant intent not found")
    if intent.status == GrantIntentStatus.APPLIED.value:
        return {"intent_id": intent.id, "status": intent.status}
    if intent.execution_run_id != body.execution_run_id:
        raise HTTPException(
            status_code=409, detail="grant intent is bound to another execution run"
        )
    if not body.active:
        if intent.kind == GrantIntentKind.INITIAL_OWNER.value and _is_exhausted(intent):
            await db.commit()
            return {"intent_id": intent.id, "status": intent.status}
        intent.status = GrantIntentStatus.RETRYABLE.value
        intent.detail = body.detail or "unverified"
        await db.commit()
        return {"intent_id": intent.id, "status": intent.status}
    if intent.kind == GrantIntentKind.INCOMING_OWNER.value:
        project = (
            await db.execute(select(Project).where(Project.id == project_id).with_for_update())
        ).scalar_one()
        if intent.outgoing_owner_id != project.owner_id or intent.incoming_owner_id is None:
            raise HTTPException(
                status_code=409, detail="project ownership changed while transfer was pending"
            )
        project.owner_id = intent.incoming_owner_id
    intent.status = GrantIntentStatus.APPLIED.value
    intent.detail = None
    intent.applied_at = datetime.now(UTC)
    await db.commit()
    return {"intent_id": intent.id, "status": intent.status}
