"""Durable permanent-access intents for generated Telegram services."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.users_grant import (
    USERS_GRANT_INTENT_KEY,
    GrantIntent,
    GrantIntentKind,
    GrantIntentStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.models import Application, Deployment, Project, Repository, Run, User
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


async def _live_target(db: AsyncSession, project_id: uuid.UUID) -> tuple[int, int, str]:
    """Return the one healthy, attributable application target for an access intent."""
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


async def _stage_intent(  # noqa: PLR0913
    db: AsyncSession,
    project: Project,
    *,
    target_user: User,
    kind: GrantIntentKind,
    actor: str,
) -> tuple[Run, DeployMessage, bool]:
    """Write the intent and deploy Run before returning its queue message."""
    application_id, deployment_id, head_sha = await _live_target(db, project.id)
    run_id = _intent_id(kind, project.id, target_user.telegram_id)
    existing = await db.get(Run, run_id)
    if existing is not None:
        stored = (existing.run_metadata or {}).get(USERS_GRANT_INTENT_KEY)
        if stored is None:
            raise HTTPException(
                status_code=409, detail="grant intent id is held by an unrelated run"
            )
        intent = GrantIntent.model_validate(stored)
        if intent.project_id != str(project.id) or intent.external_id != str(
            target_user.telegram_id
        ):
            raise HTTPException(
                status_code=409, detail="grant intent target does not match request"
            )
        recipient = await resolve_project_recipient(db, project.id, event="users_grant_resume")
        return (
            existing,
            DeployMessage(
                task_id=existing.id,
                project_id=str(project.id),
                telegram_chat_id=recipient.telegram_chat_id,
                unaddressed_reason=recipient.unaddressed_reason,
                triggered_by=DeployTrigger.PO,
                action=DeployAction.FEATURE,
                head_sha=intent.target_sha,
            ),
            False,
        )
    intent = GrantIntent(
        id=run_id,
        kind=kind,
        project_id=str(project.id),
        channel="telegram",
        external_id=str(target_user.telegram_id),
        target_application_id=application_id,
        target_deployment_id=deployment_id,
        target_sha=head_sha,
        initiating_actor=actor,
        outgoing_owner_id=project.owner_id if kind is GrantIntentKind.INCOMING_OWNER else None,
        incoming_owner_id=target_user.id if kind is GrantIntentKind.INCOMING_OWNER else None,
    )
    run = Run(
        id=run_id,
        type=RunType.DEPLOY.value,
        project_id=project.id,
        user_id=project.owner_id,
        status=RunStatus.QUEUED.value,
        run_metadata={"head_sha": head_sha, USERS_GRANT_INTENT_KEY: intent.model_dump(mode="json")},
    )
    db.add(run)
    recipient = await resolve_project_recipient(db, project.id, event="users_grant")
    return (
        run,
        DeployMessage(
            task_id=run.id,
            project_id=str(project.id),
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            triggered_by=DeployTrigger.PO,
            action=DeployAction.FEATURE,
            head_sha=head_sha,
        ),
        True,
    )


async def _publish_staged_intent(
    db: AsyncSession, redis: RedisStreamClient, run: Run, message: DeployMessage
) -> GrantIntent:
    """Publish only after the durable intent is committed; repeats use the same Run."""
    await db.commit()
    try:
        await redis.publish_message(DEPLOY_QUEUE, message)
    except Exception:
        raise HTTPException(
            status_code=503, detail="grant intent is durable but dispatch is still owed"
        ) from None
    intent = GrantIntent.model_validate(run.run_metadata[USERS_GRANT_INTENT_KEY])
    if intent.status is GrantIntentStatus.PUBLISH_OWED:
        run.run_metadata = {
            **run.run_metadata,
            USERS_GRANT_INTENT_KEY: intent.with_status(GrantIntentStatus.QUEUED).model_dump(
                mode="json"
            ),
        }
        await db.commit()
    return GrantIntent.model_validate(run.run_metadata[USERS_GRANT_INTENT_KEY])


@router.post("/{project_id}/users/grant")
async def grant_user(
    project_id: uuid.UUID,
    body: GrantUserRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    project = await load_locked_project(db, project_id)
    actor = await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    user = await _verified_user(db, body.telegram_id)
    run, message, created = await _stage_intent(
        db,
        project,
        target_user=user,
        kind=GrantIntentKind.ADD_USER,
        actor=f"user:{actor.id}" if actor is not None else "internal_service",
    )
    intent = await _publish_staged_intent(db, redis, run, message)
    logger.info(
        "users_grant_intent_staged",
        intent_id=intent.id,
        created=created,
        project_id=str(project_id),
    )
    return {"intent_id": intent.id, "status": intent.status.value, "created": created}


@router.post("/{project_id}/ownership-transfer")
async def transfer_ownership(
    project_id: uuid.UUID,
    body: OwnershipTransferRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    project = await load_locked_project(db, project_id)
    actor = await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )
    user = await _verified_user(db, body.telegram_id)
    run, message, created = await _stage_intent(
        db,
        project,
        target_user=user,
        kind=GrantIntentKind.INCOMING_OWNER,
        actor=f"user:{actor.id}" if actor is not None else "internal_service",
    )
    intent = await _publish_staged_intent(db, redis, run, message)
    return {"intent_id": intent.id, "status": intent.status.value, "created": created}


@router.post("/{project_id}/ownership-transfer/{run_id}/apply")
async def apply_transfer(
    project_id: uuid.UUID,
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    _internal: None = Depends(require_internal_or_admin),
) -> dict:
    """Atomically record active readback and move ownership to the incoming identity."""
    project = (
        await db.execute(select(Project).where(Project.id == project_id).with_for_update())
    ).scalar_one_or_none()
    run = (
        await db.execute(select(Run).where(Run.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if project is None or run is None:
        raise HTTPException(status_code=404, detail="project or grant intent not found")
    intent = GrantIntent.model_validate((run.run_metadata or {}).get(USERS_GRANT_INTENT_KEY))
    if intent.kind is not GrantIntentKind.INCOMING_OWNER or intent.project_id != str(project_id):
        raise HTTPException(
            status_code=409, detail="run is not this project's incoming-owner intent"
        )
    if intent.outgoing_owner_id != project.owner_id:
        raise HTTPException(
            status_code=409, detail="project ownership changed while transfer was pending"
        )
    if intent.incoming_owner_id is None:
        raise HTTPException(status_code=409, detail="incoming owner is missing")
    project.owner_id = intent.incoming_owner_id
    run.run_metadata = {
        **run.run_metadata,
        USERS_GRANT_INTENT_KEY: intent.with_status(GrantIntentStatus.APPLIED).model_dump(
            mode="json"
        ),
    }
    await db.commit()
    return {"intent_id": intent.id, "status": GrantIntentStatus.APPLIED.value}
