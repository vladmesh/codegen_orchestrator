"""Project bot-audience routes."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from shared.redis.client import RedisStreamClient

from ...database import get_async_session
from ...dependencies import _optional_bearer_scheme, get_redis_client, is_internal_service
from ...schemas import BotAccessRequest, BotUserMutationRequest
from ...utils.bot_audience import AudienceOperation
from .._bot_access import (
    AudienceSelection,
    mutate_bot_audience,
    owe_rollout_notification,
    rollout_status,
)

router = APIRouter()


def _mutation_response(outcome) -> dict:
    """One wire shape for every mutation: the write and the rollout apart."""
    return {
        "mode": outcome.mode,
        "operation": outcome.operation_value,
        "audience": outcome.audience,
        "rollout": outcome.rollout_status.value,
        "rollout_run_id": outcome.rollout_run_id,
    }


@router.post("/{project_id}/config/bot-access")
async def set_bot_access(
    project_id: uuid.UUID,
    body: BotAccessRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Store the selected bot audience as a deploy-time contract literal.

    Same orchestration as the one-ID mutations: authorization and the
    read-modify-write under the project row lock, then — when a bot is already
    running — a configuration-only rollout of the deployed commit. The response
    reports the rollout separately from the write; `pending` means the running
    service has NOT been confirmed to carry this audience yet.
    """
    outcome, _staged = await mutate_bot_audience(
        db,
        project_id,
        redis,
        x_telegram_id=x_telegram_id,
        is_internal=_is_internal,
        credentials=credentials,
        operation=AudienceOperation.SET,
        selection=AudienceSelection(
            mode=body.mode,
            audience=body.allowed_telegram_ids,
            allow_ownerless_audience=body.allow_ownerless_audience,
        ),
    )
    return {
        "mode": outcome.mode,
        "allowed_telegram_ids": outcome.audience,
        "rollout": outcome.rollout_status.value,
        "rollout_run_id": outcome.rollout_run_id,
    }


@router.post("/{project_id}/config/bot-access/users")
async def add_bot_user(
    project_id: uuid.UUID,
    body: BotUserMutationRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Add one Telegram ID to the chosen bot audience.

    Atomic under the project row lock, so concurrent mutations cannot lose IDs.
    Idempotent: adding an ID already present persists nothing new and launches
    no second rollout — but an unfinished rollout from an earlier attempt is
    reconciled by the scheduler sweep either way.
    """
    outcome, _staged = await mutate_bot_audience(
        db,
        project_id,
        redis,
        x_telegram_id=x_telegram_id,
        is_internal=_is_internal,
        credentials=credentials,
        operation=AudienceOperation.ADD,
        telegram_id=body.telegram_id,
    )
    return _mutation_response(outcome)


@router.delete("/{project_id}/config/bot-access/users/{telegram_id}")
async def remove_bot_user(
    project_id: uuid.UUID,
    telegram_id: int,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Remove one Telegram ID from the chosen bot audience.

    Removing the final allowed ID is refused: an empty private audience is the
    public bot, and going public must be an explicit set_bot_access decision,
    never the side effect of a removal. Removing an absent ID succeeds without
    writing or rolling out — the state already matches the request.
    """
    if telegram_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="telegram_id must be a positive integer",
        )
    outcome, _staged = await mutate_bot_audience(
        db,
        project_id,
        redis,
        x_telegram_id=x_telegram_id,
        is_internal=_is_internal,
        credentials=credentials,
        operation=AudienceOperation.REMOVE,
        telegram_id=telegram_id,
    )
    return _mutation_response(outcome)


@router.get("/{project_id}/config/bot-access/rollouts/{run_id}")
async def get_bot_audience_rollout(
    project_id: uuid.UUID,
    run_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Where a config-only audience rollout stands.

    The run must belong to *this project*, and the canonical project access
    check runs on top — so another owner's run id reads exactly like a missing
    one. The answer comes from the durable deploy run: applied once the worker
    recorded success, failed with its error, pending otherwise.
    """
    rollout, detail = await rollout_status(
        db,
        project_id,
        run_id,
        x_telegram_id=x_telegram_id,
        is_internal=_is_internal,
        credentials=credentials,
    )
    return {"rollout": rollout.value, "detail": detail}


@router.post("/{project_id}/config/bot-access/rollouts/{run_id}/notify-owed")
async def owe_bot_audience_rollout_notification(
    project_id: uuid.UUID,
    run_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Record that this rollout's terminal outcome still has to reach the owner.

    Owner-checked like every other project route. Idempotent: a repeat never
    resets a delivery already made.
    """
    return await owe_rollout_notification(
        db,
        project_id,
        run_id,
        x_telegram_id=x_telegram_id,
        is_internal=_is_internal,
        credentials=credentials,
    )
