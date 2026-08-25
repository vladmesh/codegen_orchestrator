"""Applications router — runtime state of deployed units."""

from datetime import UTC
import secrets
from urllib.parse import urlparse
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.run import RunType
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.contracts.queues.qa import QAMessage
from shared.models import (
    Application,
    Deployment,
    PortAllocation,
    Project,
    Repository,
    Run,
    Server,
)
from shared.queues import DEPLOY_QUEUE, QA_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_redis_client
from ..schemas import (
    ApplicationCreate,
    ApplicationHealthHistoryCreate,
    ApplicationHealthHistoryRead,
    ApplicationRead,
    ApplicationUpdate,
    FromRepoRequest,
)
from ..schemas.actions import AdminAction
from ..schemas.repository import RepositoryRead
from ..schemas.run import RunRead
from ..utils.telegram_binding import release_bot_binding
from ..work_admission import start_paid_run
from ._ownership import initiating_run_or_conflict
from ._recipients import ProjectRecipient, resolve_project_chat_id

logger = structlog.get_logger()

router = APIRouter(prefix="/applications", tags=["applications"])
_GITHUB_REPO_PATH_PARTS = 2


@router.post("/", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(
    app_in: ApplicationCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Create a new application record."""
    application = Application(
        repo_id=app_in.repo_id,
        server_handle=app_in.server_handle,
        service_name=app_in.service_name,
        reserved_ram_mb=app_in.reserved_ram_mb,
        status=app_in.status,
    )
    db.add(application)
    await db.commit()
    await db.refresh(application)
    return application


@router.get("/", response_model=list[ApplicationRead])
async def list_applications(
    server_handle: str | None = Query(None),
    status: str | None = Query(None),
    repo_id: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[Application]:
    """List applications with optional filtering."""
    query = select(Application)

    if server_handle is not None:
        query = query.where(Application.server_handle == server_handle)
    if status is not None:
        query = query.where(Application.status == status)
    if repo_id is not None:
        query = query.where(Application.repo_id == repo_id)

    query = query.order_by(Application.service_name)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{application_id}", response_model=ApplicationRead)
async def get_application(
    application_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Get application by ID."""
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


async def _release_bot_if_undeployed(application: Application, db: AsyncSession) -> None:
    """not_deployed is where an undeploy lands, so the repo stops holding its bot.

    Keyed on the resulting status rather than on the undeploy request: the token is
    freed once the teardown reports back, not while the bot may still be running on
    the server. A repeated patch finds the binding already gone and does nothing.

    The project has to be empty, not just this application: a project deployed on
    several servers has one of them running the bot, and the row says nothing about
    which. Releasing on the first application to report down would hand out a token
    that is still being polled.
    """
    if application.status != ApplicationStatus.NOT_DEPLOYED.value:
        return
    repo = await db.get(Repository, application.repo_id)
    project = await db.get(Project, repo.project_id)

    still_up = (
        await db.execute(
            select(Application.id)
            .join(Repository, Application.repo_id == Repository.id)
            .where(
                Repository.project_id == repo.project_id,
                Application.status != ApplicationStatus.NOT_DEPLOYED.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if still_up is not None:
        return

    repos = list(
        (
            await db.execute(select(Repository).where(Repository.project_id == repo.project_id))
        ).scalars()
    )
    await release_bot_binding(db, project, repos, reason="application_undeployed")


@router.patch("/{application_id}", response_model=ApplicationRead)
async def update_application(
    application_id: int,
    app_update: ApplicationUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Update application status and metadata."""
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if app_update.status is not None:
        application.status = app_update.status
        await _release_bot_if_undeployed(application, db)
    if app_update.last_health_check is not None:
        application.last_health_check = app_update.last_health_check
    if app_update.response_time_ms is not None:
        application.response_time_ms = app_update.response_time_ms
    if app_update.ssl_expires_at is not None:
        application.ssl_expires_at = app_update.ssl_expires_at
    if app_update.uptime_pct_24h is not None:
        application.uptime_pct_24h = app_update.uptime_pct_24h

    await db.commit()
    await db.refresh(application)
    return application


# ---------------------------------------------------------------------------
# Health history endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{application_id}/health-history",
    response_model=list[ApplicationHealthHistoryRead],
)
async def get_health_history(
    application_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_async_session),
) -> list:
    """Get health check history for an application."""
    from datetime import datetime, timedelta

    from shared.models import ApplicationHealthHistory

    if not await db.get(Application, application_id):
        raise HTTPException(status_code=404, detail="Application not found")

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(ApplicationHealthHistory)
        .where(
            ApplicationHealthHistory.application_id == application_id,
            ApplicationHealthHistory.recorded_at >= cutoff,
        )
        .order_by(ApplicationHealthHistory.recorded_at.desc())
    )

    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/health-history")
async def delete_old_health_history(
    retention_hours: int = 168,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Delete health history older than retention_hours (default 7 days)."""
    from datetime import datetime, timedelta

    from sqlalchemy import delete as sa_delete

    from shared.models import ApplicationHealthHistory

    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    stmt = sa_delete(ApplicationHealthHistory).where(ApplicationHealthHistory.recorded_at < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount}


@router.post(
    "/{application_id}/health-history",
    response_model=ApplicationHealthHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_health_history(
    application_id: int,
    snapshot: ApplicationHealthHistoryCreate,
    db: AsyncSession = Depends(get_async_session),
) -> object:
    """Append a health history snapshot for an application (internal use)."""
    from shared.models import ApplicationHealthHistory

    if not await db.get(Application, application_id):
        raise HTTPException(status_code=404, detail="Application not found")

    entry = ApplicationHealthHistory(
        application_id=application_id,
        metrics=snapshot.metrics,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Helper: load application with repo → project chain
# ---------------------------------------------------------------------------


async def _get_app_with_repo(
    application_id: int, db: AsyncSession
) -> tuple[Application, Repository]:
    """Load application and its linked repository. Raises 404 if not found."""
    query = (
        select(Application)
        .options(selectinload(Application.port_allocations))
        .where(Application.id == application_id)
    )
    result = await db.execute(query)
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")

    repo = await db.get(Repository, app.repo_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Repository {app.repo_id} not found for application {application_id}",
        )
    return app, repo


def _make_deploy_run_id() -> str:
    return f"deploy-{uuid.uuid4().hex[:12]}"


# Why an admin action on an application reports to nobody in Telegram: these
# endpoints act on one deployed application on an operator's request, not on a
# story the owner asked about, and the operator reads the API response. Stated
# rather than left as an empty recipient, so it cannot be mistaken for a producer
# that forgot to resolve one.
ADMIN_ACTION_UNADDRESSED_REASON = (
    "admin-initiated application action, reported to the operator that requested it"
)


UNDEPLOYABLE_STATUSES = frozenset(
    {
        ApplicationStatus.RUNNING,
        ApplicationStatus.STOPPED,
        ApplicationStatus.DOWN,
        ApplicationStatus.DEGRADED,
    }
)


def stage_undeploy(
    application: Application,
    project_id: uuid.UUID,
    db: AsyncSession,
    *,
    triggered_by: DeployTrigger,
    recipient: ProjectRecipient,
) -> tuple[Run, DeployMessage]:
    """Move an application to undeploying and build the message that tears it down.

    Mutates the session without committing and does not publish: the caller owns
    the transaction, and the message must not reach the deploy consumer before the
    Run it names is committed.

    *recipient* is the caller's answer to who hears about this teardown: a
    teardown the owner asked for carries their chat, an operator's carries the
    reason it does not.
    """
    application.status = ApplicationStatus.UNDEPLOYING
    run_id = _make_deploy_run_id()
    # The application id on the run is how a caller waiting for this teardown finds
    # out it failed: a failed run is the only signal an application stuck in
    # undeploying ever gets.
    run = Run(
        id=run_id,
        type="deploy",
        project_id=project_id,
        run_metadata={"application_id": application.id},
    )
    db.add(run)
    msg = DeployMessage(
        task_id=run_id,
        project_id=str(project_id),
        telegram_chat_id=recipient.telegram_chat_id,
        unaddressed_reason=recipient.unaddressed_reason,
        triggered_by=triggered_by,
        action=DeployAction.UNDEPLOY,
        application_id=application.id,
    )
    return run, msg


def _parse_github_repo_url(git_url: str) -> tuple[str, str]:
    if git_url.startswith("git@github.com:"):
        path = git_url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(git_url)
        if parsed.netloc != "github.com":
            raise ValueError(f"Repository URL is not a GitHub URL: {git_url}")
        path = parsed.path.lstrip("/")

    parts = path.rstrip("/").removesuffix(".git").split("/")
    if len(parts) != _GITHUB_REPO_PATH_PARTS or not all(parts):
        raise ValueError(f"Repository URL does not identify owner/repo: {git_url}")
    return parts[0], parts[1]


async def _resolve_admin_deploy_head_sha(repo: Repository) -> str:
    try:
        owner, repo_name = _parse_github_repo_url(repo.git_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    try:
        head_sha = await GitHubAppClient().get_default_branch_head_sha(owner, repo_name)
    except httpx.HTTPStatusError as exc:
        detail = (
            f"Could not resolve head SHA for {owner}/{repo_name}: "
            f"GitHub returned {exc.response.status_code}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not resolve head SHA for {owner}/{repo_name}: {exc}",
        ) from exc

    if not head_sha:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not resolve head SHA for {owner}/{repo_name}",
        )
    return head_sha


# ---------------------------------------------------------------------------
# Admin action endpoints
# ---------------------------------------------------------------------------


@router.post("/{application_id}/stop", response_model=ApplicationRead)
async def stop_application(
    application_id: int,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> Application:
    """Stop a running application. Publishes DeployMessage(action=STOP)."""
    body = body or AdminAction()
    app, repo = await _get_app_with_repo(application_id, db)

    if app.status in (ApplicationStatus.STOPPING, ApplicationStatus.STOPPED):
        logger.info("application_stop_already_requested", app_id=application_id, actor=body.actor)
        return app

    if app.status != ApplicationStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot stop application in status '{app.status}'. Must be running.",
        )

    app.status = ApplicationStatus.STOPPING
    run_id = _make_deploy_run_id()
    run = Run(
        id=run_id,
        type="deploy",
        project_id=repo.project_id,
        run_metadata={"application_id": app.id},
    )
    db.add(run)
    await db.commit()
    await db.refresh(app)

    msg = DeployMessage(
        task_id=run_id,
        project_id=str(repo.project_id),
        unaddressed_reason=ADMIN_ACTION_UNADDRESSED_REASON,
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.STOP,
        application_id=app.id,
    )
    await redis.publish_message(DEPLOY_QUEUE, msg)

    logger.info("application_stop_requested", app_id=application_id, actor=body.actor)
    return app


@router.post("/{application_id}/undeploy", response_model=ApplicationRead)
async def undeploy_application(
    application_id: int,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> Application:
    """Undeploy an application. Publishes DeployMessage(action=UNDEPLOY)."""
    body = body or AdminAction()
    app, repo = await _get_app_with_repo(application_id, db)

    if ApplicationStatus(app.status) not in UNDEPLOYABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot undeploy application in status '{app.status}'.",
        )

    _run, msg = stage_undeploy(
        app,
        repo.project_id,
        db,
        triggered_by=DeployTrigger.ADMIN,
        recipient=ProjectRecipient(unaddressed_reason=ADMIN_ACTION_UNADDRESSED_REASON),
    )
    await db.commit()
    await db.refresh(app)

    await redis.publish_message(DEPLOY_QUEUE, msg)

    logger.info("application_undeploy_requested", app_id=application_id, actor=body.actor)
    return app


@router.post("/{application_id}/redeploy", response_model=ApplicationRead)
async def redeploy_application(
    application_id: int,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> Application:
    """Redeploy an application. Creates Deployment record, publishes DeployMessage."""
    body = body or AdminAction()
    app, repo = await _get_app_with_repo(application_id, db)
    head_sha = await _resolve_admin_deploy_head_sha(repo)

    port = app.port_allocations[0].port if app.port_allocations else 0

    app.status = ApplicationStatus.DEPLOYING
    run_id = _make_deploy_run_id()

    deployment = Deployment(
        application_id=app.id,
        project_id=repo.project_id,
        service_name=app.service_name,
        server_handle=app.server_handle,
        port=port,
    )
    run = Run(id=run_id, type="deploy", project_id=repo.project_id)
    db.add(deployment)
    db.add(run)
    await db.commit()
    await db.refresh(app)

    msg = DeployMessage(
        task_id=run_id,
        project_id=str(repo.project_id),
        unaddressed_reason=ADMIN_ACTION_UNADDRESSED_REASON,
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.CREATE,
        head_sha=head_sha,
    )
    await redis.publish_message(DEPLOY_QUEUE, msg)

    logger.info("application_redeploy_requested", app_id=application_id, actor=body.actor)
    return app


@router.post("/{application_id}/run-e2e")
async def run_e2e(
    application_id: int,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> dict:
    """Run E2E tests on a deployed application.

    Creates a Run and publishes QAMessage to qa:queue.
    """
    body = body or AdminAction()
    app, repo = await _get_app_with_repo(application_id, db)

    if app.status != ApplicationStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot run E2E on application in status '{app.status}'. Must be running.",
        )

    # Resolve deployed URL from server IP + port
    server = await db.get(Server, app.server_handle)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Server {app.server_handle} not found",
        )

    port = app.port_allocations[0].port if app.port_allocations else 0
    deployed_url = f"http://{server.public_ip}:{port}" if port else f"http://{server.public_ip}"

    # QA validates against the repository's criteria — reject before creating the
    # Run, so a project without them doesn't leave a QA run that can only error.
    acceptance_criteria = (repo.acceptance_criteria or "").strip()
    if not acceptance_criteria:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Repository {repo.id} has no acceptance_criteria. Cannot run QA.",
        )

    # The QA executor this leads to belongs to the run the project was created
    # for, exactly as a developer worker does. Resolved before the Run row for
    # the same reason as the criteria above: a project that cannot own a worker
    # must not leave a QA run behind that nothing will ever pick up.
    project = await db.get(Project, repo.project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {repo.project_id} not found",
        )
    initiating_run_id = initiating_run_or_conflict(project)

    run_id = f"qa-{uuid.uuid4().hex[:12]}"
    started = await start_paid_run(
        PaidRunStartCommand(
            id=run_id,
            type=RunType.QA,
            project_id=repo.project_id,
            run_metadata={"triggered_by": "admin", "application_id": application_id},
        ),
        db,
    )
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        await db.commit()
        return {"admission": started.admission.model_dump(mode="json")}
    await db.commit()
    run = await db.get(Run, run_id)
    if run is None:
        raise RuntimeError("Paid QA run disappeared before publication")
    await db.refresh(run)
    await db.refresh(app)

    msg = QAMessage(
        project_id=str(repo.project_id),
        initiating_run_id=initiating_run_id,
        telegram_chat_id=await resolve_project_chat_id(db, repo.project_id, event="qa_run"),
        deployed_url=deployed_url,
        application_id=application_id,
        acceptance_criteria=acceptance_criteria,
        run_id=run_id,
        bot_username=repo.bot_username,
    )
    await redis.publish_message(QA_QUEUE, msg)

    logger.info("e2e_requested", app_id=application_id, run_id=run_id, actor=body.actor)
    return {
        "application": ApplicationRead.model_validate(app, from_attributes=True),
        "run": RunRead.model_validate(run, from_attributes=True),
    }


@router.get("/{application_id}/runs", response_model=list[RunRead])
async def list_application_runs(
    application_id: int,
    run_type: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_session),
) -> list[Run]:
    """List runs associated with an application (stored in run_metadata)."""
    from sqlalchemy import String, cast

    query = (
        select(Run)
        .where(cast(Run.run_metadata["application_id"].as_string(), String) == str(application_id))
        .order_by(Run.created_at.desc())
        .limit(limit)
    )
    if run_type:
        query = query.where(Run.type == run_type)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/from-repo", status_code=status.HTTP_201_CREATED)
async def create_from_repo(
    body: FromRepoRequest,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> dict:
    """Create application from an existing repository and trigger deploy.

    Creates Repository (if needed), Application, allocates a port,
    and publishes DeployMessage to deploy:queue.
    """
    # Verify server exists
    server = await db.get(Server, body.server_handle)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Server {body.server_handle} not found",
        )

    # Find or create repository
    query = select(Repository).where(
        Repository.git_url == body.repo_url,
        Repository.project_id == body.project_id,
    )
    result = await db.execute(query)
    repo = result.scalar_one_or_none()

    if not repo:
        repo_name = body.repo_url.rstrip("/").rsplit("/", maxsplit=1)[-1].removesuffix(".git")
        repo = Repository(
            id=f"repo-{secrets.token_hex(4)}",
            project_id=body.project_id,
            name=repo_name,
            git_url=body.repo_url,
            is_managed=False,
        )
        db.add(repo)
        await db.flush()

    head_sha = await _resolve_admin_deploy_head_sha(repo)

    # Create application
    app = Application(
        repo_id=repo.id,
        server_handle=body.server_handle,
        service_name=body.service_name,
        status=ApplicationStatus.DEPLOYING.value,
    )
    db.add(app)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Application already exists for this repo + server combination",
        ) from exc

    # Allocate port (find next available starting from 8000)
    port_query = (
        select(PortAllocation.port)
        .where(PortAllocation.server_handle == body.server_handle)
        .with_for_update()
    )
    port_result = await db.execute(port_query)
    allocated_ports = {row[0] for row in port_result.all()}
    port = 8000
    while port in allocated_ports:
        port += 1

    allocation = PortAllocation(
        server_handle=body.server_handle,
        port=port,
        service_name=body.service_name,
        application_id=app.id,
    )
    db.add(allocation)

    # Create Run
    run_id = _make_deploy_run_id()
    run = Run(id=run_id, type="deploy", project_id=body.project_id)
    db.add(run)

    await db.commit()
    await db.refresh(app)
    await db.refresh(repo)

    # Publish deploy message
    msg = DeployMessage(
        task_id=run_id,
        project_id=str(body.project_id),
        unaddressed_reason=ADMIN_ACTION_UNADDRESSED_REASON,
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.CREATE,
        head_sha=head_sha,
    )
    await redis.publish_message(DEPLOY_QUEUE, msg)

    logger.info(
        "application_created_from_repo",
        app_id=app.id,
        repo_id=repo.id,
        server=body.server_handle,
        port=port,
        actor=body.actor,
    )
    return {
        "application": ApplicationRead.model_validate(app, from_attributes=True),
        "repository": RepositoryRead.model_validate(repo, from_attributes=True),
    }
