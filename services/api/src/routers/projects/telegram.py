"""Telegram-token binding and liveness routes."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.repository import RepositoryRole
from shared.contracts.dto.telegram import (
    BotLiveness,
    BotLivenessState,
    TelegramTokenValidateRequest,
    TelegramTokenVerdict,
    TokenVerdictStatus,
)
from shared.crypto import decrypt_dict
from shared.models import Project, Repository

from ...database import get_async_session
from ...dependencies import (
    _optional_bearer_scheme,
    is_internal_service,
    require_internal_or_admin,
)
from ...utils.telegram_binding import TELEGRAM_TOKEN_KEY, TELEGRAM_USERNAME_KEY
from ...utils.telegram_token import bot_liveness, validate_telegram_token
from ..projects_guards import check_project_access
from .secrets import _merge_secrets_into_project

logger = structlog.get_logger()
router = APIRouter()


@router.post("/{project_id}/telegram/token", response_model=TelegramTokenVerdict)
async def bind_telegram_token(
    project_id: uuid.UUID,
    body: TelegramTokenValidateRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> TelegramTokenVerdict:
    """Validate a Telegram bot token and, if it passes, bind it to the project.

    The only way TELEGRAM_BOT_TOKEN gets into a project's secrets. A rejected token
    is not stored; the verdict carries a user-facing message the PO agent voices.
    """
    query = select(Project).where(Project.id == project_id).with_for_update()
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    verdict = await validate_telegram_token(body.token, db=db, project=project)
    if verdict.status == TokenVerdictStatus.REJECTED:
        logger.info(
            "telegram_token_rejected",
            project_id=str(project_id),
            reason_code=verdict.reason_code,
        )
        return verdict

    # bot_username lives on the primary repository — QA reads it from there, so a
    # missing repository is a hard error, not a silently skipped write.
    repo_query = select(Repository).where(
        Repository.project_id == project_id,
        Repository.role == RepositoryRole.PRIMARY.value,
    )
    repo = (await db.execute(repo_query)).scalars().first()
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project {project_id} has no primary repository — cannot bind a bot token",
        )

    _merge_secrets_into_project(
        project,
        {
            TELEGRAM_TOKEN_KEY: body.token.strip(),
            TELEGRAM_USERNAME_KEY: verdict.bot_username,
        },
        {
            TELEGRAM_TOKEN_KEY: "Telegram bot token from @BotFather",
            TELEGRAM_USERNAME_KEY: (
                "Bot username (without @) for building t.me links and smoke tests"
            ),
        },
    )
    repo.bot_username = verdict.bot_username
    await db.commit()

    logger.info(
        "telegram_token_bound",
        project_id=str(project_id),
        bot_username=verdict.bot_username,
    )
    return verdict


@router.get("/{project_id}/telegram/liveness", response_model=BotLiveness)
async def check_telegram_bot_liveness(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_session),
    _internal_or_admin: None = Depends(require_internal_or_admin),
) -> BotLiveness:
    """Ask Telegram whether this project's bot is live, without lending the token.

    QA has to know that the deployed bot answers at the moment it tests it, and
    the token that proves it belongs here: it is stored encrypted in this
    project's secrets and this service holds the key. So the question is asked
    here and only the answer travels — a state, the username Telegram reported,
    and a detail line. Handing the token to the QA runtime instead would put a
    live credential in a runtime whose whole design is that it holds none.

    Internal or admin only: it spends a call on Telegram's API using a secret the
    caller cannot see, which is a platform action, not a project read.
    """
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stored = (project.config or {}).get("secrets") or {}
    token = decrypt_dict(stored).get(TELEGRAM_TOKEN_KEY) if stored else None
    if not token:
        return BotLiveness(
            state=BotLivenessState.NO_TOKEN,
            detail=f"project {project_id} holds no {TELEGRAM_TOKEN_KEY}",
        )

    liveness = await bot_liveness(token)
    logger.info(
        "telegram_bot_liveness_checked",
        project_id=str(project_id),
        state=liveness.state.value,
        bot_username=liveness.bot_username,
    )
    return liveness


"""Telegram-token binding and liveness routes."""
