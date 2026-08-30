"""Project configuration and encrypted-secret routes."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.crypto import decrypt_dict, encrypt_dict
from shared.models import Project

from ...database import get_async_session
from ...dependencies import _optional_bearer_scheme, is_internal_service
from ...schemas import MergeSecretsRequest
from ...utils.telegram_binding import TELEGRAM_TOKEN_KEY
from ...utils.telegram_token import looks_like_bot_token
from ..projects_guards import check_project_access

logger = structlog.get_logger()
router = APIRouter()
delete_router = APIRouter()


@router.get("/{project_id}/config/secrets/keys")
async def list_secret_keys(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """List secret key names for a project (no values exposed)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    config = dict(project.config or {})
    existing_secrets = config.get("secrets") or {}
    existing_secrets = decrypt_dict(existing_secrets) if existing_secrets else {}

    return {"keys": sorted(existing_secrets.keys())}


_TELEGRAM_TOKEN_DETAIL = (
    f"{TELEGRAM_TOKEN_KEY} cannot be set directly — "
    "POST the token to /api/projects/{project_id}/telegram/token so it is validated first"
)


_SECRETS_WRITE_DETAIL = (
    "config.secrets is not writable through this endpoint — "
    "use /config/secrets to set and /config/secrets/{key} to delete"
)


def _reject_bot_token_writes(secrets: dict[str, str]) -> None:
    """Keep bot tokens off the generic secret path — they go through the validator."""
    for key, value in secrets.items():
        if key == TELEGRAM_TOKEN_KEY or looks_like_bot_token(value):
            raise HTTPException(status_code=422, detail=_TELEGRAM_TOKEN_DETAIL)


def _find_bot_token_material(node: object, path: str = "config") -> str | None:
    """Locate a Telegram token key or token-shaped value anywhere in a config tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == TELEGRAM_TOKEN_KEY:
                return f"{path}.{key}"
            found = _find_bot_token_material(value, f"{path}.{key}")
            if found:
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found = _find_bot_token_material(item, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(node, str) and looks_like_bot_token(node):
        return path
    return None


def _stored_secrets_blob(project: Project | None) -> dict:
    if project is None:
        return {}
    return (project.config or {}).get("secrets") or {}


def _vet_config_write(config: dict, project: Project | None) -> dict:
    """Validate a whole-config write and return the config to store.

    Secrets are owned by the dedicated endpoints: the stored (encrypted) blob is
    carried over untouched, and a caller that sends a different one is refused
    rather than silently overwritten. Everything else in the tree is scanned so a
    raw bot token can't ride in under another key.
    """
    stored = _stored_secrets_blob(project)
    incoming = config.get("secrets")
    if incoming is not None and incoming != stored:
        raise HTTPException(status_code=422, detail=_SECRETS_WRITE_DETAIL)

    # env_hints keys are env var names, so TELEGRAM_BOT_TOKEN is expected there;
    # only its values (plaintext descriptions) are scanned for token material.
    hints = config.get("env_hints")
    found = _find_bot_token_material(
        {key: value for key, value in config.items() if key not in ("secrets", "env_hints")}
    )
    if found is None and isinstance(hints, dict):
        found = _find_bot_token_material(list(hints.values()), "config.env_hints")
    if found is not None:
        raise HTTPException(status_code=422, detail=f"{_TELEGRAM_TOKEN_DETAIL} (found at {found})")

    vetted = {key: value for key, value in config.items() if key != "secrets"}
    if stored:
        vetted["secrets"] = stored
    stored_config = project.config if project is not None else {}
    stored_agent_type = stored_config.get("agent_type") if isinstance(stored_config, dict) else None
    if stored_agent_type is not None and vetted.get("agent_type") is None:
        vetted["agent_type"] = stored_agent_type
    return vetted


def _merge_secrets_into_project(
    project: Project,
    secrets: dict[str, str],
    env_hints: dict[str, str] | None,
) -> list[str]:
    """Merge secrets into the project's config in place. Returns all secret keys."""
    config = dict(project.config or {})
    existing_secrets = config.get("secrets") or {}
    existing_secrets = decrypt_dict(existing_secrets) if existing_secrets else {}

    existing_secrets.update(secrets)
    config["secrets"] = encrypt_dict(existing_secrets)

    if env_hints:
        merged_hints = config.get("env_hints") or {}
        merged_hints.update(env_hints)
        config["env_hints"] = merged_hints

    project.config = config
    return sorted(existing_secrets.keys())


@router.post("/{project_id}/config/secrets")
async def merge_secrets(
    project_id: uuid.UUID,
    body: MergeSecretsRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Atomically merge secrets into project config.

    Uses SELECT FOR UPDATE to prevent race conditions when multiple
    callers set secrets concurrently.
    """
    if not body.secrets:
        raise HTTPException(
            status_code=422,
            detail="secrets must not be empty",
        )

    _reject_bot_token_writes(body.secrets)
    # Lock the row to prevent concurrent read-modify-write
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

    keys = _merge_secrets_into_project(project, body.secrets, body.env_hints)
    await db.commit()

    logger.info(
        "secrets_merged",
        project_id=project_id,
        keys=sorted(body.secrets.keys()),
    )

    return {"keys": keys}


@delete_router.delete("/{project_id}/config/secrets/{key}")
async def delete_secret(
    project_id: uuid.UUID,
    key: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> dict:
    """Delete a single secret from project config.

    Uses SELECT FOR UPDATE to prevent race conditions.
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

    config = dict(project.config or {})
    existing_secrets = config.get("secrets") or {}
    existing_secrets = decrypt_dict(existing_secrets) if existing_secrets else {}

    if key not in existing_secrets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Secret key '{key}' not found",
        )

    del existing_secrets[key]
    config["secrets"] = encrypt_dict(existing_secrets) if existing_secrets else {}

    project.config = config
    await db.commit()

    logger.info("secret_deleted", project_id=project_id, key=key)
    return {"keys": sorted(existing_secrets.keys())}
