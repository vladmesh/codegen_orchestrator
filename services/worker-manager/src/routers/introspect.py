"""Read-only introspection API for admin panel.

Exposes worker status, logs, and workspace files.
All data comes from Redis metadata + Docker + host filesystem.
"""

from http import HTTPStatus
from pathlib import Path
from typing import Any

import docker
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.worker_turn import AttemptTurnMetadata, WorkerActiveTurn, active_turn_key
from shared.queues import STORY_WORKERS_KEY
from shared.redis import decode_redis_fields

from ..config import settings
from ._shared import FileTreeEntry, read_file, walk_workspace

logger = structlog.get_logger()

router = APIRouter(prefix="/api/introspect", tags=["introspect"])

MAX_LOG_TAIL = 5000
DEFAULT_LOG_TAIL = 100


# --- Response models ---


class WorkerSummary(BaseModel):
    id: str
    status: str
    project_id: str | None = None
    repo_id: str | None = None
    workspace_path: str | None = None
    dev_network: str | None = None
    error: str | None = None
    container: "ContainerFact | None" = None
    container_error: str | None = None
    agent_process_status: str | None = None
    agent_process_status_error: str | None = None
    active_turn_lease: "ActiveTurnLease | None" = None
    active_turn_lease_error: str | None = None
    story_bindings: list[str] = []
    story_bindings_error: str | None = None
    attempt_run: "AttemptRun | None" = None
    attempt_run_error: str | None = None
    waiting_attempt: "WaitingAttempt | None" = None
    waiting_attempt_error: str | None = None


class ContainerFact(BaseModel):
    id: str
    image: str | None = None
    state: str | None = None


class ActiveTurnLease(BaseModel):
    attempt_id: str
    request_id: str
    lease_id: str
    started_at: str
    deadline_at: str


class WaitingAttempt(BaseModel):
    run_id: str
    run_status: str
    request_id: str
    requested_at: str | None = None


class AttemptRun(BaseModel):
    id: str
    status: str


class WorkerDetail(WorkerSummary):
    container_id: str | None = None
    image: str | None = None


class _InventoryContext(BaseModel):
    attempt_runs: dict[str, AttemptRun] = {}
    waiting_attempts: dict[str, WaitingAttempt] = {}
    story_bindings: dict[str, list[str]] = {}
    attempts_error: str | None = None
    story_bindings_error: str | None = None


class WorkerLogsResponse(BaseModel):
    worker_id: str
    logs: str
    tail: int


class FileContentResponse(BaseModel):
    worker_id: str
    path: str
    content: str
    size: int


# --- Helpers ---


async def _check_worker_exists(redis, worker_id: str) -> dict:
    """Check worker exists in Redis, raise 404 if not."""
    status_data = decode_redis_fields(await redis.hgetall(f"worker:status:{worker_id}"))
    if not status_data:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Worker not found")
    return status_data


async def _get_workspace_path(redis, worker_id: str, request: Request | None = None) -> Path:
    """Get workspace path for a worker.

    Resolves via repo_id from Redis metadata → SCAFFOLDED_WORKSPACE_PATH/{repo_id}/.
    Both `repo_id` and `workspace_path` are written conditionally when the
    container is created, so a worker that carries no repo_id — or whose
    scaffolded directory is not on this host — is resolved by the stored path.
    """
    meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
    repo_id = meta.get("repo_id")

    if repo_id and request:
        scaffolded_base = getattr(request.app.state, "scaffolded_workspace_path", None)
        if scaffolded_base:
            workspace = Path(scaffolded_base) / repo_id
            if workspace.exists() and workspace.is_dir():
                return workspace

    # No repo_id, or its scaffolded directory is absent: use the stored path.
    workspace_path = meta.get("workspace_path")
    if not workspace_path:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Workspace path not found in worker metadata",
        )
    path = Path(workspace_path)
    if not path.exists():
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Workspace directory does not exist",
        )
    return path


async def _inventory_context(request: Request) -> _InventoryContext:
    """Read facts which need the existing API Run and story-worker records."""
    context = _InventoryContext()
    attempts = getattr(request.app.state, "engineering_attempts", None)
    if attempts is None:
        context.attempts_error = "running engineering attempts unavailable"
    else:
        try:
            active_runs = await attempts.list_running()
        except Exception as exc:
            logger.warning("worker_inventory_attempts_unavailable", error_type=type(exc).__name__)
            context.attempts_error = "running engineering attempts unavailable"
        else:
            for run in active_runs:
                run_id = run.get("id")
                run_status = run.get("status")
                if not isinstance(run_id, str) or not isinstance(run_status, str):
                    logger.warning("worker_inventory_attempt_identity_invalid")
                    continue
                context.attempt_runs[run_id] = AttemptRun(id=run_id, status=run_status)
                try:
                    turn = AttemptTurnMetadata.from_run_metadata(run.get("run_metadata"))
                except Exception:
                    logger.warning("worker_inventory_attempt_metadata_invalid", run_id=run.get("id"))
                    continue
                if turn.worker_id is None or turn.active_turn_request_id is None:
                    continue
                context.waiting_attempts[turn.worker_id] = WaitingAttempt(
                    run_id=run_id,
                    run_status=run_status,
                    request_id=turn.active_turn_request_id,
                    requested_at=(
                        turn.active_turn_requested_at.isoformat() if turn.active_turn_requested_at is not None else None
                    ),
                )

    try:
        bindings = decode_redis_fields(await request.app.state.redis.hgetall(STORY_WORKERS_KEY))
    except Exception as exc:
        logger.warning("worker_inventory_bindings_unavailable", error_type=type(exc).__name__)
        context.story_bindings_error = "story bindings are unreadable"
    else:
        for story_id, worker_id in bindings.items():
            context.story_bindings.setdefault(worker_id, []).append(story_id)
    return context


async def _container_fact(docker_client, worker_id: str) -> tuple[ContainerFact | None, str | None]:
    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
    try:
        attrs = await docker_client.inspect_container(container_name)
    except docker.errors.NotFound:
        return None, None
    except Exception as exc:
        logger.warning("worker_inventory_container_unreadable", worker_id=worker_id, error_type=type(exc).__name__)
        return None, "container is unreadable"
    container_id = attrs.get("Id")
    if not isinstance(container_id, str):
        return None, "container record is invalid"
    config = attrs.get("Config")
    state = attrs.get("State")
    return (
        ContainerFact(
            id=container_id,
            image=config.get("Image") if isinstance(config, dict) else None,
            state=state.get("Status") if isinstance(state, dict) else None,
        ),
        None,
    )


async def _inventory_fields(
    redis,
    worker_id: str,
    attempt_id: str | None,
    context: _InventoryContext,
    *,
    attempt_id_error: str | None = None,
) -> dict[str, Any]:
    lease_error = None
    lease = None
    try:
        active_turn = WorkerActiveTurn.from_redis_fields(
            decode_redis_fields(await redis.hgetall(active_turn_key(worker_id)))
        )
        if active_turn is not None:
            lease = ActiveTurnLease(
                attempt_id=active_turn.attempt_id,
                request_id=active_turn.request_id,
                lease_id=active_turn.lease_id,
                started_at=active_turn.started_at.isoformat(),
                deadline_at=active_turn.deadline_at.isoformat(),
            )
    except Exception:
        lease_error = "active turn lease is invalid or unreadable"
        logger.warning("worker_inventory_active_turn_unreadable", worker_id=worker_id)
    return {
        "active_turn_lease": lease,
        "active_turn_lease_error": lease_error,
        "story_bindings": context.story_bindings.get(worker_id, []),
        "story_bindings_error": context.story_bindings_error,
        "attempt_run": context.attempt_runs.get(attempt_id) if attempt_id and not context.attempts_error else None,
        "attempt_run_error": attempt_id_error or context.attempts_error,
        "waiting_attempt": context.waiting_attempts.get(worker_id),
        "waiting_attempt_error": context.attempts_error,
    }


# --- Endpoints ---


@router.get("/workers/", response_model=list[WorkerSummary])
async def list_workers(request: Request):
    """List all known workers with their status and metadata."""
    redis = request.app.state.redis
    docker = request.app.state.docker
    keys = await redis.keys("worker:status:*")
    context = await _inventory_context(request)

    workers = []
    for key in keys:
        worker_id = key.split(":", 2)[2]
        status_error = None
        try:
            status_data = decode_redis_fields(await redis.hgetall(f"worker:status:{worker_id}"))
        except Exception as exc:
            logger.warning(
                "worker_inventory_agent_status_unreadable", worker_id=worker_id, error_type=type(exc).__name__
            )
            status_data = {}
            status_error = "agent process status is unreadable"
        meta_error = None
        try:
            meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
        except Exception as exc:
            logger.warning("worker_inventory_metadata_unreadable", worker_id=worker_id, error_type=type(exc).__name__)
            meta = {}
            meta_error = "worker metadata is unreadable"
        error = await redis.get(f"worker:error:{worker_id}")

        # Docker and the worker process report different facts. Preserve both.
        redis_status = status_data.get("status", WorkerStatus.UNKNOWN)
        container, container_error = await _container_fact(docker, worker_id)
        if container is None and container_error is None:
            if redis_status == WorkerStatus.RUNNING:
                redis_status = WorkerStatus.GONE

        workers.append(
            WorkerSummary(
                id=worker_id,
                status=redis_status,
                project_id=meta.get("project_id"),
                repo_id=meta.get("repo_id"),
                workspace_path=meta.get("workspace_path"),
                dev_network=meta.get("dev_network"),
                error=error,
                container=container,
                container_error=container_error,
                agent_process_status=status_data.get("status"),
                agent_process_status_error=status_error,
                **await _inventory_fields(
                    redis,
                    worker_id,
                    meta.get("attempt_id"),
                    context,
                    attempt_id_error=meta_error,
                ),
            )
        )

    return workers


@router.get("/workers/{worker_id}", response_model=WorkerDetail)
async def get_worker(worker_id: str, request: Request):
    """Get detailed worker info including container details."""
    redis = request.app.state.redis
    docker = request.app.state.docker

    status_data = await _check_worker_exists(redis, worker_id)
    meta_error = None
    try:
        meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
    except Exception as exc:
        logger.warning("worker_inventory_metadata_unreadable", worker_id=worker_id, error_type=type(exc).__name__)
        meta = {}
        meta_error = "worker metadata is unreadable"
    error = await redis.get(f"worker:error:{worker_id}")
    context = await _inventory_context(request)

    container_id = None
    image = None
    redis_status = status_data.get("status", WorkerStatus.UNKNOWN)
    container, container_error = await _container_fact(docker, worker_id)
    if container is not None:
        container_id = container.id
        image = container.image
    elif container_error is None:
        if redis_status == "RUNNING":
            redis_status = "GONE"

    return WorkerDetail(
        id=worker_id,
        status=redis_status,
        project_id=meta.get("project_id"),
        repo_id=meta.get("repo_id"),
        workspace_path=meta.get("workspace_path"),
        dev_network=meta.get("dev_network"),
        error=error,
        container_id=container_id,
        image=image,
        container=container,
        container_error=container_error,
        agent_process_status=status_data.get("status"),
        **await _inventory_fields(
            redis,
            worker_id,
            meta.get("attempt_id"),
            context,
            attempt_id_error=meta_error,
        ),
    )


@router.get("/workers/{worker_id}/logs", response_model=WorkerLogsResponse)
async def get_worker_logs(
    worker_id: str,
    request: Request,
    tail: int = Query(default=DEFAULT_LOG_TAIL, ge=1, le=MAX_LOG_TAIL),
):
    """Get recent container logs."""
    redis = request.app.state.redis
    docker = request.app.state.docker

    await _check_worker_exists(redis, worker_id)

    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
    logs = await docker.get_container_logs(container_name, tail=tail)

    return WorkerLogsResponse(worker_id=worker_id, logs=logs, tail=tail)


@router.get("/workers/{worker_id}/tree", response_model=list[FileTreeEntry])
async def get_worker_tree(worker_id: str, request: Request):
    """List files in the worker's workspace directory."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)
    workspace = await _get_workspace_path(redis, worker_id, request)
    return walk_workspace(workspace)


@router.get("/workers/{worker_id}/files/{file_path:path}", response_model=FileContentResponse)
async def get_worker_file(worker_id: str, file_path: str, request: Request):
    """Read a file from the worker's workspace."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)
    workspace = await _get_workspace_path(redis, worker_id, request)
    content, size = read_file(workspace, file_path)
    return FileContentResponse(
        worker_id=worker_id,
        path=file_path,
        content=content,
        size=size,
    )


@router.delete("/workers/{worker_id}", status_code=HTTPStatus.NO_CONTENT)
async def kill_worker(worker_id: str, request: Request):
    """Force-kill a worker container and clean up resources."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)

    worker_manager = request.app.state.worker_manager
    await worker_manager.delete_worker(worker_id, reason="admin_kill")
