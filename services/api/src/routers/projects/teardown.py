"""Project teardown and destructive deletion routes."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.bot_rollout import BOT_ROLLOUT_METADATA_KEY
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus, ProjectTeardownResult, TeardownStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.queues.deploy import DeployTrigger
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
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, SCAFFOLD_QUEUE
from shared.redis.client import RedisStreamClient

from ...config import get_settings
from ...database import get_async_session
from ...dependencies import (
    _optional_bearer_scheme,
    get_redis_client,
    is_internal_service,
)
from ...utils.telegram_binding import release_bot_binding
from .._recipients import resolve_project_recipient
from ..applications import UNDEPLOYABLE_STATUSES, stage_undeploy
from ..projects_guards import check_project_access

logger = structlog.get_logger()
router = APIRouter()


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

    await check_project_access(
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

    await check_project_access(
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
