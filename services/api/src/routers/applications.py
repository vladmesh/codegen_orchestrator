"""Applications router — runtime state of deployed units."""

from datetime import UTC
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.contracts.queues.qa import QAMessage
from shared.models import Application, Deployment, PortAllocation, Repository, Run, Server
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

logger = structlog.get_logger()

router = APIRouter(prefix="/applications", tags=["applications"])


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

    if app.status != ApplicationStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot stop application in status '{app.status}'. Must be running.",
        )

    app.status = ApplicationStatus.STOPPING
    run_id = _make_deploy_run_id()
    run = Run(id=run_id, type="deploy", project_id=repo.project_id)
    db.add(run)
    await db.commit()
    await db.refresh(app)

    msg = DeployMessage(
        task_id=run_id,
        project_id=str(repo.project_id),
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.STOP,
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

    active_statuses = {
        ApplicationStatus.RUNNING,
        ApplicationStatus.STOPPED,
        ApplicationStatus.DOWN,
        ApplicationStatus.DEGRADED,
    }
    if ApplicationStatus(app.status) not in active_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot undeploy application in status '{app.status}'.",
        )

    app.status = ApplicationStatus.UNDEPLOYING
    run_id = _make_deploy_run_id()
    run = Run(id=run_id, type="deploy", project_id=repo.project_id)
    db.add(run)
    await db.commit()
    await db.refresh(app)

    msg = DeployMessage(
        task_id=run_id,
        project_id=str(repo.project_id),
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.UNDEPLOY,
    )
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
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.CREATE,
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

    # Create Run
    run_id = f"qa-{uuid.uuid4().hex[:12]}"
    run = Run(
        id=run_id,
        type="qa",
        project_id=repo.project_id,
        run_metadata={"triggered_by": "admin", "application_id": application_id},
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await db.refresh(app)

    # Publish QA message
    msg = QAMessage(
        project_id=str(repo.project_id),
        user_id="",
        deployed_url=deployed_url,
        application_id=application_id,
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
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.CREATE,
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
