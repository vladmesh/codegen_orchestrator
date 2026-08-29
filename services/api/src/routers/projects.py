"""Projects router.

Bot-audience endpoints (set/add/remove + rollout status) delegate to
`._bot_access`, which owns the shared mutation/rollout orchestration; the
guards they all share live in `.projects_guards`.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.bot_rollout import BOT_ROLLOUT_METADATA_KEY
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus, ProjectTeardownResult, TeardownStatus
from shared.contracts.dto.repository import RepositoryRole
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.telegram import (
    BotLiveness,
    BotLivenessState,
    TelegramTokenValidateRequest,
    TelegramTokenVerdict,
    TokenVerdictStatus,
)
from shared.contracts.dto.work_admission import WorkAdmissionOutcome
from shared.contracts.queues.deploy import DeployTrigger
from shared.contracts.vocab import AgentType
from shared.crypto import decrypt_dict, encrypt_dict
from shared.models import (
    AnalyticsDaily,
    AnalyticsHourly,
    AnalyticsKnownUsers,
    Application,
    ApplicationHealthHistory,
    Brainstorm,
    Deployment,
    PortAllocation,
    Project,
    RAGChunk,
    RAGConversationSummary,
    RAGDocument,
    RAGMessage,
    Repository,
    Run,
    Story,
    Task,
    TaskEvent,
)
from shared.project_slug import generate_project_slug
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, SCAFFOLD_QUEUE
from shared.redis.client import RedisStreamClient

from ..config import get_settings
from ..database import get_async_session
from ..dependencies import (
    _optional_bearer_scheme,
    get_redis_client,
    is_internal_service,
    require_internal_or_admin,
    resolve_actor,
)
from ..schemas import (
    BotAccessRequest,
    BotUserMutationRequest,
    MergeSecretsRequest,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
)
from ..utils.bot_audience import AudienceOperation
from ..utils.telegram_binding import TELEGRAM_TOKEN_KEY, TELEGRAM_USERNAME_KEY, release_bot_binding
from ..utils.telegram_token import bot_liveness, looks_like_bot_token, validate_telegram_token
from ..work_admission import admit_project_creation
from ._bot_access import (
    mutate_bot_audience,
    owe_rollout_notification,
    rollout_status,
)
from ._recipients import resolve_project_recipient
from .applications import UNDEPLOYABLE_STATUSES, stage_undeploy

logger = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])

_LEGACY_BOT_AUDIENCE_KEY = "ADMIN_TELEGRAM_ID"
_BOT_ACCESS_WRITE_DETAIL = "bot access is managed through /config/bot-access"


def _mutation_response(outcome) -> dict:
    """One wire shape for every mutation: the write and the rollout apart."""
    return {
        "mode": outcome.mode,
        "operation": outcome.operation_value,
        "audience": outcome.audience,
        "rollout": outcome.rollout_status.value,
        "rollout_run_id": outcome.rollout_run_id,
    }


async def _check_project_access(
    project: Project,
    telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool = False,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Check if the request may reach this project. Raises 401/403/404 if denied.

    Who is acting is `resolve_actor`'s decision, not this function's: a service
    acting for itself passes, a named user is judged as that user however the
    request was authenticated.
    """
    actor = await resolve_actor(
        is_internal=is_internal,
        telegram_id=telegram_id,
        credentials=credentials,
        db=db,
    )

    if actor is None or actor.is_admin:
        return

    # Regular user: must be owner; unowned projects are admin-only
    if project.owner_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not project owner",
        )


async def _load_locked_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    """Load one project under the lock used by every config writer."""
    query = select(Project).where(Project.id == project_id).with_for_update()
    project = (await db.execute(query)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _initial_project_config(config: dict) -> dict:
    """Resolve the developer agent exactly once, when a project is created."""
    resolved = dict(config)
    explicit_agent_type = resolved.get("agent_type")
    if explicit_agent_type is None:
        resolved["agent_type"] = get_settings().default_agent_type.value
        return resolved

    try:
        resolved["agent_type"] = AgentType(explicit_agent_type).value
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported agent_type: {explicit_agent_type}",
        ) from exc
    return resolved


@router.post("/", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    project_in: ProjectCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Create a new project."""
    try:
        project_id = project_in.id or uuid.uuid4()

        logger.info(
            "creating_project",
            project_id=str(project_id),
            title=project_in.title,
            status=project_in.status,
            telegram_id=x_telegram_id,
        )

        # Check if ID exists
        if project_in.id and await db.get(Project, project_id):
            logger.warning("project_creation_failed_duplicate", project_id=str(project_id))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project with this ID already exists",
            )

        # Ownership and admission are both derived from the caller principal.
        # A bearer token names its subject; the Telegram header names an actor
        # only for an internal service caller.
        user = await resolve_actor(
            is_internal=_is_internal,
            telegram_id=x_telegram_id,
            credentials=credentials,
            db=db,
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Telegram-ID header is required",
            )
        owner_id = user.id

        admission = await admit_project_creation(user.id, user.is_admin, db)
        if admission.outcome is not WorkAdmissionOutcome.ADMITTED:
            await db.commit()
            logger.info(
                "project_creation_admission_denied",
                owner_id=user.id,
                reason=admission.reason.value if admission.reason else None,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"admission": admission.model_dump(mode="json")},
            )

        project = Project(
            id=project_id,
            title=project_in.title,
            slug=generate_project_slug(project_in.title, project_id),
            status=project_in.status.value,
            config=_vet_config_write(_initial_project_config(project_in.config), None),
            owner_id=owner_id,
            # Ownership enters the system here and nowhere else: the caller that
            # started the run names it, and every worker created for this
            # project is stamped with it later.
            initiating_run_id=project_in.initiating_run_id,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        logger.info(
            "project_created",
            project_id=str(project.id),
            status=project.status,
            owner_id=owner_id,
        )

        return project

    except HTTPException:
        raise
    except Exception as e:
        # The creation body carries project secrets, so it never reaches the log
        # stream. Its size is enough to tell an empty request from a truncated one.
        try:
            body_size = len(await request.body())
        except Exception:
            body_size = -1

        logger.error(
            "project_creation_failed",
            error=str(e),
            error_type=type(e).__name__,
            request_body_bytes=body_size,
            telegram_id=x_telegram_id,
        )
        raise


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )
    return project


@router.get("/", response_model=list[ProjectRead])
async def list_projects(
    # alias keeps the public query param name; a parameter literally named
    # `status` would shadow the fastapi.status module used below
    project_status: str | None = Query(None, alias="status"),
    owner_id: int | None = None,
    owner_only: bool = False,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> list[Project]:
    """List projects, optionally filtered by status or owner_id."""
    # The caller is resolved before any filter is applied, including the admin
    # panel's owner_id filter: passing owner_id must not be a way to read another
    # user's projects and their config.
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
    )

    query = select(Project)

    if actor is None:
        # Service call: no user to scope against.
        if owner_id is not None:
            query = query.where(Project.owner_id == owner_id)
    elif actor.is_admin:
        if owner_id is not None:
            query = query.where(Project.owner_id == owner_id)
        elif owner_only:
            query = query.where(Project.owner_id == actor.id)
    else:
        # A regular user sees only their own projects, whatever owner_id says.
        query = query.where(Project.owner_id == actor.id)

    if project_status:
        query = query.where(Project.status == project_status)

    result = await db.execute(query)
    return list(result.scalars().all())


async def _release_bot_if_archived(db: AsyncSession, project: Project) -> None:
    """Archiving is teardown, so the project stops holding its bot.

    Keyed on the resulting status, not on the transition: archiving an already
    archived project releases nothing the first call left behind.
    """
    if project.status != ProjectStatus.ARCHIVED.value:
        return
    repos = await db.execute(select(Repository).where(Repository.project_id == project.id))
    await release_bot_binding(db, project, repos.scalars().all(), reason="project_archived")


@router.put("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Update project."""
    project = await _load_locked_project(db, project_id)

    await _check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    if project_in.title is not None:
        project.title = project_in.title
    if project_in.status is not None:
        project.status = project_in.status.value
    if project_in.config is not None:
        project.config = _vet_config_write(project_in.config, project)
    if project_in.project_spec is not None:
        project.project_spec = project_in.project_spec

    await _release_bot_if_archived(db, project)

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=str(project.id), status=project.status)

    return project


@router.patch("/{project_id}", response_model=ProjectRead)
async def patch_project(
    project_id: uuid.UUID,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await _load_locked_project(db, project_id)

    await _check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    if project_in.title is not None:
        project.title = project_in.title
    if project_in.status is not None:
        project.status = project_in.status.value
    if project_in.config is not None:
        project.config = _vet_config_write(project_in.config, project)
    if project_in.project_spec is not None:
        project.project_spec = project_in.project_spec

    await _release_bot_if_archived(db, project)

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=str(project.id), status=project.status)

    return project


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

    await _check_project_access(
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


def _reject_legacy_bot_access_writes(secrets: dict[str, str]) -> None:
    """Keep new audiences on the template contract path.

    Existing encrypted legacy values remain readable by the deploy resolver while
    projects migrate. This only rejects writes that would create or alter that
    legacy state after the contract audience endpoint exists.
    """
    if _LEGACY_BOT_AUDIENCE_KEY in secrets:
        raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)


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
    stored_access = stored_config.get("bot_access") if isinstance(stored_config, dict) else None
    stored_overrides = (
        stored_config.get("env_overrides", {}) if isinstance(stored_config, dict) else {}
    )
    stored_audience = (
        stored_overrides.get("TG_BOT_ALLOWED_TELEGRAM_IDS")
        if isinstance(stored_overrides, dict)
        else None
    )
    incoming_access = config.get("bot_access")
    incoming_overrides = config.get("env_overrides", {})

    if stored_access is not None:
        if incoming_access is not None and incoming_access != stored_access:
            raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)
        vetted["bot_access"] = stored_access
    elif incoming_access is not None:
        raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)

    if stored_audience is not None:
        if not isinstance(incoming_overrides, dict):
            raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)
        incoming_audience = incoming_overrides.get("TG_BOT_ALLOWED_TELEGRAM_IDS")
        if incoming_audience is not None and incoming_audience != stored_audience:
            raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)
        overrides = dict(incoming_overrides)
        overrides["TG_BOT_ALLOWED_TELEGRAM_IDS"] = stored_audience
        vetted["env_overrides"] = overrides
    elif (
        isinstance(incoming_overrides, dict) and "TG_BOT_ALLOWED_TELEGRAM_IDS" in incoming_overrides
    ):
        raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)
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
    _reject_legacy_bot_access_writes(body.secrets)

    # Lock the row to prevent concurrent read-modify-write
    query = select(Project).where(Project.id == project_id).with_for_update()
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(
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
        set_mode=body.mode,
        set_audience=body.allowed_telegram_ids,
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

    await _check_project_access(
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


@router.delete("/{project_id}/config/secrets/{key}")
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

    await _check_project_access(
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

    if key == _LEGACY_BOT_AUDIENCE_KEY:
        raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)

    del existing_secrets[key]
    config["secrets"] = encrypt_dict(existing_secrets) if existing_secrets else {}

    project.config = config
    await db.commit()

    logger.info("secret_deleted", project_id=project_id, key=key)
    return {"keys": sorted(existing_secrets.keys())}


async def _load_for_teardown(
    project_id: uuid.UUID,
    x_telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[Project, list[Repository], list[Application]]:
    """Load the project, its repositories and its applications, owner-checked."""
    query = select(Project).where(Project.id == project_id).with_for_update()
    project = (await db.execute(query)).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=is_internal,
        credentials=credentials,
    )

    repos_result = await db.execute(select(Repository).where(Repository.project_id == project_id))
    repos = list(repos_result.scalars())
    apps = list(
        (
            await db.execute(
                select(Application).where(Application.repo_id.in_([repo.id for repo in repos]))
            )
        ).scalars()
    )
    return project, repos, apps


async def _stalled_undeploy_error(
    db: AsyncSession, project_id: uuid.UUID, application_id: int
) -> str | None:
    """The error of the latest undeploy run for this application, if it did not run.

    Only the latest run counts: an application that failed to come down once and is
    being torn down again is pending, not failed.

    A cancelled run counts as a failure. The deploy consumer cancels a run whose
    project lock is held by another deploy, and an application whose only undeploy
    was cancelled would otherwise sit in `undeploying` forever, with nothing to say
    so and no way for a retry to send it down again.

    Configuration-only bot-audience rollouts also write deploy runs carrying the
    same application id; they are not teardown attempts and are skipped here —
    otherwise a rollout finishing between two teardown polls would read as "the
    undeploy succeeded" or its failure as "the undeploy failed".
    """
    runs = (
        await db.execute(
            select(Run)
            .where(Run.project_id == project_id, Run.type == "deploy")
            .order_by(Run.created_at.desc())
        )
    ).scalars()
    for run in runs:
        metadata = run.run_metadata or {}
        if metadata.get("application_id") != application_id:
            continue
        if BOT_ROLLOUT_METADATA_KEY in metadata or (
            metadata.get("triggered_by") == "bot_audience_rollout"
        ):
            # Not an undeploy: this run redeployed the application.
            continue
        if run.status not in (RunStatus.FAILED.value, RunStatus.CANCELLED.value):
            return None
        return run.error_message or "undeploy failed"
    return None


async def _teardown_state(
    db: AsyncSession,
    project: Project,
    repos: list[Repository],
    applications: list[Application],
    *,
    just_staged: frozenset[int] = frozenset(),
) -> ProjectTeardownResult:
    """Report where the teardown stands, and finish it once nothing is left up.

    Archiving and releasing the bot happen here rather than at request time: while a
    container is still up the bot is still polling its token, and handing that token
    to another project would make Telegram answer 409 to whoever asks second.

    `just_staged` names the applications whose undeploy this very request published.
    Their earlier failure, if any, is history: the run that decides their fate has
    not been picked up yet.
    """
    pending = [app.id for app in applications if app.status != ApplicationStatus.NOT_DEPLOYED.value]

    for application_id in pending:
        if application_id in just_staged:
            continue
        error = await _stalled_undeploy_error(db, project.id, application_id)
        if error:
            return ProjectTeardownResult(
                project_id=project.id,
                status=TeardownStatus.FAILED,
                project_status=ProjectStatus(project.status),
                pending_application_ids=pending,
                error=error,
            )

    if pending:
        return ProjectTeardownResult(
            project_id=project.id,
            status=TeardownStatus.PENDING,
            project_status=ProjectStatus(project.status),
            pending_application_ids=pending,
        )

    # Nothing is up any more. The undeploy path may already have released the bot
    # when the application reported not_deployed; then there is no name left to
    # report, only the fact that the project holds nothing.
    project.status = ProjectStatus.ARCHIVED.value
    released = await release_bot_binding(db, project, repos, reason="project_teardown")

    logger.info(
        "project_teardown_completed",
        project_id=str(project.id),
        released_bot=released,
    )
    return ProjectTeardownResult(
        project_id=project.id,
        status=TeardownStatus.COMPLETED,
        project_status=ProjectStatus.ARCHIVED,
        released_bot_username=released,
    )


@router.post("/{project_id}/teardown", response_model=ProjectTeardownResult)
async def teardown_project(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProjectTeardownResult:
    """Start tearing the project down at its owner's request: undeploy it, then free its bot.

    Owner-checked, unlike the per-application stop/undeploy endpoints, because this is
    the route the PO agent drives on behalf of a user. A project with nothing deployed
    is done when this returns; anything still up comes back `pending` and keeps its bot
    until the undeploy reports the containers down. Poll GET on the same path.
    """
    project, repos, applications = await _load_for_teardown(
        project_id,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    # The owner asked for this teardown, so its deploys are addressed to them:
    # resolved once, before anything is published.
    teardown_recipient = await resolve_project_recipient(db, project_id, event="project_teardown")

    messages = []
    staged = set()
    for application in applications:
        status_now = ApplicationStatus(application.status)
        if status_now not in UNDEPLOYABLE_STATUSES:
            # An application stuck in undeploying after a failed run is the one case
            # worth sending down again: the user asking a second time is a retry.
            failed = status_now == ApplicationStatus.UNDEPLOYING and await _stalled_undeploy_error(
                db, project_id, application.id
            )
            if not failed:
                continue
        _run, msg = stage_undeploy(
            application,
            project_id,
            db,
            triggered_by=DeployTrigger.PO,
            recipient=teardown_recipient,
        )
        messages.append(msg)
        staged.add(application.id)

    state = await _teardown_state(db, project, repos, applications, just_staged=frozenset(staged))
    await db.commit()

    for msg in messages:
        await redis.publish_message(DEPLOY_QUEUE, msg)

    logger.info(
        "project_teardown_requested",
        project_id=str(project_id),
        status=state.status.value,
        pending=state.pending_application_ids,
    )
    return state


@router.get("/{project_id}/teardown", response_model=ProjectTeardownResult)
async def get_teardown_status(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> ProjectTeardownResult:
    """Where the teardown stands, and the call that finishes it.

    Owner-checked like the POST. Once every application reports not_deployed this
    archives the project and releases its bot, so `completed` is the point at which
    the token can be bound somewhere else.
    """
    project, repos, applications = await _load_for_teardown(
        project_id,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )
    state = await _teardown_state(db, project, repos, applications)
    await db.commit()
    return state


_QUEUES_TO_CLEAN = [ARCHITECT_QUEUE, SCAFFOLD_QUEUE, ENGINEERING_QUEUE, DEPLOY_QUEUE]


async def _cleanup_project_queue_messages(project_id: str) -> int:
    """Remove stale queue messages referencing a deleted project.

    Scans all pipeline queues and deletes messages whose project_id matches.
    Best-effort — failures are logged but don't block project deletion.
    """
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    deleted = 0
    try:
        for queue in _QUEUES_TO_CLEAN:
            try:
                entries = await r.xrange(queue)
            except Exception as exc:
                logger.debug("queue_scan_failed", queue=queue, error=str(exc))
                continue
            for entry_id, fields in entries:
                if fields.get("project_id") == project_id:
                    await r.xdel(queue, entry_id)
                    deleted += 1
        # Also clear scaffold inflight marker
        await r.delete(f"scaffold:inflight:{project_id}")
    finally:
        await r.aclose()
    if deleted:
        logger.info("project_queue_messages_cleaned", project_id=project_id, deleted=deleted)
    return deleted


async def _delete_project_records(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Clear everything pointing at the project, children before parents.

    None of these foreign keys cascade in the database, so a row left behind fails
    the final delete. Repositories matter most for the bot binding: while the row
    survives, its bot_username still reads as taken by a project that is gone.
    """
    repo_ids_q = select(Repository.id).where(Repository.project_id == project_id)
    app_ids_q = select(Application.id).where(Application.repo_id.in_(repo_ids_q))
    task_ids_q = select(Task.id).where(Task.project_id == project_id)

    # Runs reference stories and tasks as well as the project, so they go first.
    await db.execute(delete(Run).where(Run.project_id == project_id))

    await db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids_q)))
    await db.execute(delete(Task).where(Task.project_id == project_id))
    await db.execute(delete(Story).where(Story.project_id == project_id))
    await db.execute(delete(Brainstorm).where(Brainstorm.project_id == project_id))

    for model in (AnalyticsHourly, AnalyticsDaily, AnalyticsKnownUsers):
        await db.execute(delete(model).where(model.project_id == project_id))
    for model in (RAGChunk, RAGDocument, RAGMessage, RAGConversationSummary):
        await db.execute(delete(model).where(model.project_id == project_id))

    await db.execute(
        delete(ApplicationHealthHistory).where(
            ApplicationHealthHistory.application_id.in_(app_ids_q)
        )
    )
    await db.execute(delete(Deployment).where(Deployment.application_id.in_(app_ids_q)))
    await db.execute(delete(PortAllocation).where(PortAllocation.application_id.in_(app_ids_q)))
    await db.execute(delete(Application).where(Application.repo_id.in_(repo_ids_q)))
    await db.execute(delete(Repository).where(Repository.project_id == project_id))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
):
    """Delete a project and everything that references it."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    await _delete_project_records(db, project_id)

    await db.delete(project)
    await db.commit()

    # Best-effort cleanup: remove stale queue messages for this project
    try:
        await _cleanup_project_queue_messages(str(project_id))
    except Exception as e:
        logger.warning("project_queue_cleanup_failed", project_id=project_id, error=str(e))

    logger.info("project_deleted", project_id=project_id)
