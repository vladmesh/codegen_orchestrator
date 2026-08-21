"""Projects router."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.bot_access import (
    add_to_audience,
    canonical_audience,
    parse_allowed_telegram_ids,
    remove_from_audience,
)
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
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
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
    User,
)
from shared.project_slug import generate_project_slug
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, SCAFFOLD_QUEUE
from shared.redis.client import RedisStreamClient

from ..config import get_settings
from ..database import get_async_session
from ..dependencies import (
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
from ..utils.telegram_binding import TELEGRAM_TOKEN_KEY, TELEGRAM_USERNAME_KEY, release_bot_binding
from ..utils.telegram_token import bot_liveness, looks_like_bot_token, validate_telegram_token
from ._recipients import ProjectRecipient, resolve_project_recipient
from .applications import UNDEPLOYABLE_STATUSES, stage_undeploy

logger = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])

_BOT_ALLOWED_IDS_KEY = "TG_BOT_ALLOWED_TELEGRAM_IDS"
_LEGACY_BOT_AUDIENCE_KEY = "ADMIN_TELEGRAM_ID"
_BOT_ACCESS_WRITE_DETAIL = "bot access is managed through /config/bot-access"

# Rollout statuses the audience endpoints report, in the PO-facing vocabulary:
# applied means the running service carries the new configuration; pending
# means the rollout is on its way but unconfirmed; failed means it did not land.
ROLLOUT_APPLIED = "applied"
ROLLOUT_PENDING = "pending"
ROLLOUT_FAILED = "failed"
ROLLOUT_NOT_DEPLOYED = "not_deployed"

# The deploy run id prefix of a config-only rollout. Distinct from story deploys,
# so an operator can tell one apart in the runs list at a glance.
ROLLOUT_RUN_ID_PREFIX = "botrollout-"


async def _resolve_user(
    telegram_id: int | None,
    db: AsyncSession,
) -> User | None:
    """Resolve User from telegram_id."""
    if not telegram_id:
        return None
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _check_project_access(
    project: Project,
    telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool = False,
) -> None:
    """Check if the request may reach this project. Raises 401/403/404 if denied.

    Who is acting is `resolve_actor`'s decision, not this function's: a service
    acting for itself passes, a named user is judged as that user however the
    request was authenticated.
    """
    actor = await resolve_actor(is_internal=is_internal, telegram_id=telegram_id, db=db)

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

        # Resolve owner — required
        if not x_telegram_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Telegram-ID header is required",
            )
        user = await _resolve_user(x_telegram_id, db)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with telegram_id {x_telegram_id} not found",
            )
        owner_id = user.id

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
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)
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
) -> list[Project]:
    """List projects, optionally filtered by status or owner_id."""
    # The caller is resolved before any filter is applied, including the admin
    # panel's owner_id filter: passing owner_id must not be a way to read another
    # user's projects and their config.
    actor = await resolve_actor(is_internal=_is_internal, telegram_id=x_telegram_id, db=db)

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
) -> Project:
    """Update project."""
    project = await _load_locked_project(db, project_id)

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await _load_locked_project(db, project_id)

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
) -> dict:
    """List secret key names for a project (no values exposed)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
        stored_overrides.get(_BOT_ALLOWED_IDS_KEY) if isinstance(stored_overrides, dict) else None
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
        incoming_audience = incoming_overrides.get(_BOT_ALLOWED_IDS_KEY)
        if incoming_audience is not None and incoming_audience != stored_audience:
            raise HTTPException(status_code=422, detail=_BOT_ACCESS_WRITE_DETAIL)
        overrides = dict(incoming_overrides)
        overrides[_BOT_ALLOWED_IDS_KEY] = stored_audience
        vetted["env_overrides"] = overrides
    elif isinstance(incoming_overrides, dict) and _BOT_ALLOWED_IDS_KEY in incoming_overrides:
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

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
    _is_internal: bool = Depends(is_internal_service),
) -> dict:
    """Store the selected bot audience as a deploy-time contract literal."""
    project = await _load_locked_project(db, project_id)
    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

    config = dict(project.config or {})
    overrides = dict(config.get("env_overrides") or {})
    overrides[_BOT_ALLOWED_IDS_KEY] = body.allowed_telegram_ids
    config["env_overrides"] = overrides
    config["bot_access"] = {
        "mode": body.mode,
        "allowed_telegram_ids": body.allowed_telegram_ids,
    }
    existing_secrets = config.get("secrets") or {}
    existing_secrets = decrypt_dict(existing_secrets) if existing_secrets else {}
    if _LEGACY_BOT_AUDIENCE_KEY in existing_secrets:
        del existing_secrets[_LEGACY_BOT_AUDIENCE_KEY]
        config["secrets"] = encrypt_dict(existing_secrets) if existing_secrets else {}
        logger.info("legacy_bot_access_replaced", project_id=str(project_id), mode=body.mode)
    project.config = config
    await db.commit()

    logger.info("project_bot_access_set", project_id=str(project_id), mode=body.mode)
    return {"mode": body.mode, "allowed_telegram_ids": body.allowed_telegram_ids}


# ---------------------------------------------------------------------------
# Conversational audience mutations (add/remove one Telegram ID)
# ---------------------------------------------------------------------------


def _stored_audience(config: dict) -> str | None:
    """The project's chosen audience, or None when it never chose one.

    A missing bot_access is not an empty (public) audience: a public choice is
    explicit. Returning None here is what makes an add refuse instead of
    silently turning an undecided bot private.
    """
    access = config.get("bot_access")
    if not isinstance(access, dict) or access.get("mode") == "public":
        return None
    return canonical_audience(project_bot_audience_of(config))


def project_bot_audience_of(config: dict) -> str:
    """The raw audience string this project stores, "" when there is none."""
    access = config.get("bot_access")
    if not isinstance(access, dict):
        return ""
    audience = access.get("allowed_telegram_ids")
    return audience if isinstance(audience, str) else ""


async def _find_live_rollout_target(
    db: AsyncSession, project_id: uuid.UUID
) -> tuple[int, str] | None | str:
    """The running application and the SHA it was deployed from.

    Read from the latest successful service deployment record: that is where the
    deployer wrote what actually runs, so the rollout redeploys exactly the
    commit and images already live instead of whatever the branch holds now.

    Returns None when nothing is running (a config-only write is honest), and
    the string "unrecorded" when something is running but no successful
    deployment recorded its SHA — a target we cannot safely redeploy, which the
    caller must refuse rather than quietly skip.
    """
    query = (
        select(Application.id, Deployment)
        .outerjoin(Deployment, Deployment.application_id == Application.id)
        .where(Application.status == ApplicationStatus.RUNNING)
        .order_by(Application.id, Deployment.deployed_at.desc().nulls_last())
    )
    rows = (await db.execute(query)).all()
    # Latest deployment per application; prefer the most recently deployed one.
    by_application: dict[int, tuple[int, str] | None] = {}
    for application_id, deployment in rows:
        if application_id in by_application:
            continue
        if deployment is None or not deployment.deployed_sha:
            by_application[application_id] = None
        else:
            by_application[application_id] = (
                application_id,
                deployment.deployed_sha,
            )
    if not by_application:
        return None
    unrecorded = [app for app, target in by_application.items() if target is None]
    if unrecorded:
        return "unrecorded"
    return max(by_application.values(), key=lambda pair: pair[0])


def _stage_config_rollout(
    project: Project,
    run_id: str,
    head_sha: str,
    recipient: ProjectRecipient,
) -> DeployMessage:
    """Build the config-only rollout message for *project*.

    Same commit, same images, no story, no engineering: the DevOps subgraph
    re-reads the project's env_overrides when it rebuilds the DOTENV payload,
    so publishing a plain FEATURE deploy of the recorded SHA is what carries
    the new audience to the running service.
    """
    return DeployMessage(
        task_id=run_id,
        project_id=str(project.id),
        telegram_chat_id=recipient.telegram_chat_id,
        unaddressed_reason=recipient.unaddressed_reason,
        story_id="",
        triggered_by=DeployTrigger.PO,
        action=DeployAction.FEATURE,
        head_sha=head_sha,
        env_overrides={},
    )


@router.post("/{project_id}/config/bot-access/users")
async def add_bot_user(
    project_id: uuid.UUID,
    body: BotUserMutationRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
) -> dict:
    """Add one Telegram ID to the chosen bot audience.

    Atomic under the project row lock: the stored audience is read, extended
    and written in one transaction, so concurrent mutations cannot lose IDs.
    Idempotent: adding an ID already present persists nothing new and launches
    no second rollout.
    """
    return await _mutate_bot_audience(
        project_id,
        body.telegram_id,
        _add_operation,
        "added",
        x_telegram_id=x_telegram_id,
        db=db,
        redis=redis,
        is_internal=_is_internal,
    )


@router.delete("/{project_id}/config/bot-access/users/{telegram_id}")
async def remove_bot_user(
    project_id: uuid.UUID,
    telegram_id: int,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _is_internal: bool = Depends(is_internal_service),
) -> dict:
    """Remove one Telegram ID from the chosen bot audience.

    Removing the final allowed ID is refused: an empty private audience is the
    public bot, and going public must be an explicit set_bot_access decision,
    never the side effect of a removal. Removing an absent ID succeeds without
    writing or rolling out — the state already matches the request.
    """
    return await _mutate_bot_audience(
        project_id,
        telegram_id,
        _remove_operation,
        "removed",
        x_telegram_id=x_telegram_id,
        db=db,
        redis=redis,
        is_internal=_is_internal,
    )


async def _add_operation(audience: str, telegram_id: int) -> str:
    return add_to_audience(audience, telegram_id)


async def _remove_operation(audience: str, telegram_id: int) -> str:
    updated = remove_from_audience(audience, telegram_id)
    if parse_allowed_telegram_ids(updated):
        return updated
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"Telegram ID {telegram_id} is the final allowed ID — removing it would "
            "make the bot public. Use /config/bot-access with mode='public' if that "
            "is what you want."
        ),
    )


async def _mutate_bot_audience(
    project_id: uuid.UUID,
    telegram_id: int,
    operation,
    operation_name: str,
    *,
    x_telegram_id: int | None,
    db: AsyncSession,
    redis: RedisStreamClient,
    is_internal: bool,
) -> dict:
    """Apply one typed ID mutation to the stored audience, then roll out if live.

    The whole read-modify-write happens under `_load_locked_project`, the same
    lock every other config writer takes, and the transaction commits before any
    queue message is published — a rollout never names a run the database does
    not have yet. The response reports the rollout separately from the write:
    `pending` means the configuration changed but the running service has not
    been confirmed to carry it yet.
    """
    project = await _load_locked_project(db, project_id)
    await _check_project_access(project, x_telegram_id, db, is_internal=is_internal)

    config = dict(project.config or {})
    stored = _stored_audience(config)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "this project has no private bot audience to change — choose one with "
                "/config/bot-access first"
                if config.get("bot_access", {}).get("mode") == "public"
                else "no bot audience has been chosen for this project — set one with "
                "/config/bot-access before adding or removing users"
            ),
        )

    updated = await operation(stored, telegram_id)

    unchanged = updated == stored
    if unchanged:
        # The state already matches the request: no write, no commit, and no
        # rollout — an idempotent repeat launches nothing.
        logger.info(
            "project_bot_audience_mutated",
            project_id=str(project_id),
            operation=operation_name,
            telegram_id=telegram_id,
            audience=stored,
            idempotent=True,
            rollout=ROLLOUT_NOT_DEPLOYED,
            rollout_run_id=None,
        )
        return {
            "mode": (config.get("bot_access") or {}).get("mode"),
            "operation": "already_present" if operation_name == "added" else "already_absent",
            "audience": stored,
            "rollout": ROLLOUT_NOT_DEPLOYED,
            "rollout_run_id": None,
        }

    overrides = dict(config.get("env_overrides") or {})
    overrides[_BOT_ALLOWED_IDS_KEY] = updated
    access = dict(config["bot_access"])
    access["allowed_telegram_ids"] = updated
    config["env_overrides"] = overrides
    config["bot_access"] = access
    project.config = config

    recipient = await resolve_project_recipient(
        db, project_id, event=f"bot_audience_{operation_name}"
    )

    rollout_run_id: str | None = None
    rollout_status = ROLLOUT_NOT_DEPLOYED
    message: DeployMessage | None = None
    if not unchanged:
        target = await _find_live_rollout_target(db, project_id)
        if isinstance(target, str):
            # Something is running but no successful deployment recorded its SHA,
            # so there is no commit to redeploy and no safe rollout exists. The
            # config change is rolled back with the transaction: the caller is
            # told, not left believing the audience will reach the service.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "the bot is running but its deployed commit is not recorded — a "
                    "configuration-only rollout cannot be started safely; redeploy "
                    "the project first"
                ),
            )
        if target is not None:
            application_id, head_sha = target
            rollout_run_id = f"{ROLLOUT_RUN_ID_PREFIX}{uuid.uuid4().hex[:12]}"
            run = Run(
                id=rollout_run_id,
                type="deploy",
                project_id=project_id,
                run_metadata={
                    "application_id": application_id,
                    "head_sha": head_sha,
                    "triggered_by": "bot_audience_rollout",
                },
            )
            db.add(run)
            message = _stage_config_rollout(project, rollout_run_id, head_sha, recipient)
            rollout_status = ROLLOUT_PENDING

    await db.commit()

    if message is not None:
        await redis.publish_message(DEPLOY_QUEUE, message)

    logger.info(
        "project_bot_audience_mutated",
        project_id=str(project_id),
        operation=operation_name,
        telegram_id=telegram_id,
        audience=updated,
        idempotent=False,
        rollout=rollout_status,
        rollout_run_id=rollout_run_id,
    )

    return {
        "mode": (config.get("bot_access") or {}).get("mode"),
        "operation": operation_name,
        "audience": updated,
        "rollout": rollout_status,
        "rollout_run_id": rollout_run_id,
    }


@router.get("/{project_id}/config/bot-access/rollouts/{run_id}")
async def get_bot_audience_rollout(
    run_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
) -> dict:
    """Where a config-only audience rollout stands.

    Reads the durable deploy run the mutation created, so the answer survives
    restarts: applied only once the deploy worker recorded success, failed with
    its error when it did not, pending while it is still on its way.
    """
    result = await db.execute(select(Run).where(Run.id == run_id))
    run = result.scalars().first()
    if run is None:
        raise HTTPException(status_code=404, detail="Rollout run not found")

    if run.status == RunStatus.COMPLETED.value:
        rollout, detail = ROLLOUT_APPLIED, ""
    elif run.status == RunStatus.FAILED.value:
        outcome = (run.result or {}).get("deploy_outcome")
        rollout = ROLLOUT_FAILED
        detail = run.error_message or f"deploy outcome: {outcome or 'failed'}"
    else:
        rollout, detail = ROLLOUT_PENDING, ""

    return {"rollout": rollout, "detail": detail}


@router.post("/{project_id}/telegram/token", response_model=TelegramTokenVerdict)
async def bind_telegram_token(
    project_id: uuid.UUID,
    body: TelegramTokenValidateRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
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

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
) -> dict:
    """Delete a single secret from project config.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    query = select(Project).where(Project.id == project_id).with_for_update()
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

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
) -> tuple[Project, list[Repository], list[Application]]:
    """Load the project, its repositories and its applications, owner-checked."""
    query = select(Project).where(Project.id == project_id).with_for_update()
    project = (await db.execute(query)).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db, is_internal=is_internal)

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
    """
    runs = (
        await db.execute(
            select(Run)
            .where(Run.project_id == project_id, Run.type == "deploy")
            .order_by(Run.created_at.desc())
        )
    ).scalars()
    for run in runs:
        if (run.run_metadata or {}).get("application_id") != application_id:
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
) -> ProjectTeardownResult:
    """Start tearing the project down at its owner's request: undeploy it, then free its bot.

    Owner-checked, unlike the per-application stop/undeploy endpoints, because this is
    the route the PO agent drives on behalf of a user. A project with nothing deployed
    is done when this returns; anything still up comes back `pending` and keeps its bot
    until the undeploy reports the containers down. Poll GET on the same path.
    """
    project, repos, applications = await _load_for_teardown(
        project_id, x_telegram_id, db, is_internal=_is_internal
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
) -> ProjectTeardownResult:
    """Where the teardown stands, and the call that finishes it.

    Owner-checked like the POST. Once every application reports not_deployed this
    archives the project and releases its bot, so `completed` is the point at which
    the token can be bound somewhere else.
    """
    project, repos, applications = await _load_for_teardown(
        project_id, x_telegram_id, db, is_internal=_is_internal
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
):
    """Delete a project and everything that references it."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db, is_internal=_is_internal)

    await _delete_project_records(db, project_id)

    await db.delete(project)
    await db.commit()

    # Best-effort cleanup: remove stale queue messages for this project
    try:
        await _cleanup_project_queue_messages(str(project_id))
    except Exception as e:
        logger.warning("project_queue_cleanup_failed", project_id=project_id, error=str(e))

    logger.info("project_deleted", project_id=project_id)
