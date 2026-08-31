"""API-owned lifecycle for durable generated-service permanent-access intents."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.users_grant import (
    USERS_GRANT_INTENT_KEY,
    GrantIntent,
    GrantIntentDispatchTarget,
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
    GrantIntentStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
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
from ..projects_guards import check_project_access, load_locked_project

router = APIRouter()
logger = structlog.get_logger()
DEPLOY_RETRY_CEILING_KEY = "deploy.max_deploy_retries"
_RETRY_CEILING_EXHAUSTED_DETAIL = "deployment retry ceiling exhausted"


class GrantIntentLifecycleRequest(BaseModel):
    """Internal producer request. It never accepts a capability or secret."""

    kind: GrantIntentKind
    story_id: str | None = None
    head_sha: str | None = Field(default=None, min_length=40, max_length=40)


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


async def _verified_user(db: AsyncSession, telegram_id: int) -> User:
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="verified Telegram identity not found")
    return user


def _intent_id(kind: GrantIntentKind, project_id: uuid.UUID, telegram_id: int) -> str:
    return f"users-grant-{kind.value}-{project_id.hex}-{telegram_id}"


def _as_dto(intent: UsersGrantIntent) -> GrantIntent:
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
    )


def _target_changed(intent: UsersGrantIntent, target: tuple[int | None, int | None, str]) -> bool:
    return (intent.target_application_id, intent.target_deployment_id, intent.target_sha) != target


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


async def _lifecycle(  # noqa: PLR0913
    db: AsyncSession,
    project: Project,
    *,
    target_user: User,
    kind: GrantIntentKind,
    actor: str,
    target: tuple[int | None, int | None, str],
    story_id: str | None,
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
        )
        db.add(intent)
        await db.flush()
    elif intent.status == GrantIntentStatus.APPLIED.value:
        return intent, None, False, GrantIntentLifecycleDisposition.ALREADY_APPLIED
    rebound = False
    if intent is not None and _target_changed(intent, target):
        intent.target_history = [
            *(intent.target_history or []),
            {
                "application_id": intent.target_application_id,
                "deployment_id": intent.target_deployment_id,
                "sha": intent.target_sha,
                "replaced_at": datetime.now(UTC).isoformat(),
            },
        ]
        intent.target_application_id, intent.target_deployment_id, intent.target_sha = target
        intent.status = GrantIntentStatus.PUBLISH_OWED.value
        intent.detail = None
        rebound = True

    live_run = None if rebound else await _execution_is_live(db, intent)
    if live_run is not None:
        return intent, live_run, False, GrantIntentLifecycleDisposition.IN_FLIGHT

    if intent.attempts >= await _deploy_retry_ceiling(db):
        intent.status = GrantIntentStatus.FAILED.value
        intent.detail = _RETRY_CEILING_EXHAUSTED_DETAIL
        return intent, None, created, GrantIntentLifecycleDisposition.EXHAUSTED

    run = Run(
        id=f"deploy-grant-{uuid.uuid4().hex}",
        type=RunType.DEPLOY.value,
        project_id=project.id,
        user_id=project.owner_id,
        story_id=story_id,
        status=RunStatus.QUEUED.value,
        run_metadata={
            "head_sha": target[2],
            "triggered_by": "users_grant_intent",
            "deploy_action": (
                DeployAction.CREATE.value if target[0] is None else DeployAction.FEATURE.value
            ),
            USERS_GRANT_INTENT_KEY: intent.id,
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
    if run is not None and intent.status == GrantIntentStatus.PUBLISH_OWED.value:
        recipient = await resolve_project_recipient(db, project.id, event="users_grant_intent")
        message = DeployMessage(
            task_id=run.id,
            project_id=str(project.id),
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            triggered_by=DeployTrigger.PO,
            action=DeployAction(run.run_metadata["deploy_action"]),
            head_sha=intent.target_sha,
        )
        await db.commit()
        try:
            await redis.publish_message(DEPLOY_QUEUE, message)
        except Exception:
            raise HTTPException(
                status_code=503, detail="grant intent is durable but dispatch is still owed"
            ) from None
        if intent.status == GrantIntentStatus.PUBLISH_OWED.value:
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
        story_id=None,
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
        story_id=body.story_id,
    )
    return await _dispatch_lifecycle(db, redis, project, intent, run, disposition, created)


@router.get("/{project_id}/users/grant-intents/{intent_id}")
async def get_intent(
    project_id: uuid.UUID,
    intent_id: str,
    db: AsyncSession = Depends(get_async_session),
    _internal: None = Depends(require_internal_or_admin),
) -> GrantIntent:
    intent = await db.get(UsersGrantIntent, intent_id)
    if intent is None or intent.project_id != project_id:
        raise HTTPException(status_code=404, detail="grant intent not found")
    return _as_dto(intent)


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
