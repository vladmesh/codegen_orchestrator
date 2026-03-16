"""Read-only introspection API for admin panel.

Exposes worker status, logs, and workspace files.
All data comes from Redis metadata + Docker + host filesystem.
"""

from http import HTTPStatus
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from shared.contracts.dto.worker import WorkerStatus

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
    last_activity: str | None = None
    error: str | None = None


class WorkerDetail(WorkerSummary):
    container_id: str | None = None
    image: str | None = None


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
    status_data = await redis.hgetall(f"worker:status:{worker_id}")
    if not status_data:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Worker not found")
    return status_data


async def _get_workspace_path(redis, worker_id: str, request: Request | None = None) -> Path:
    """Get workspace path for a worker.

    Resolves via repo_id from Redis metadata → SCAFFOLDED_WORKSPACE_PATH/{repo_id}/.
    Falls back to workspace_path from Redis metadata for legacy workers.
    """
    meta = await redis.hgetall(f"worker:meta:{worker_id}")
    repo_id = meta.get("repo_id")

    if repo_id and request:
        scaffolded_base = getattr(request.app.state, "scaffolded_workspace_path", None)
        if scaffolded_base:
            workspace = Path(scaffolded_base) / repo_id
            if workspace.exists() and workspace.is_dir():
                return workspace

    # Fallback: workspace_path from Redis metadata (legacy workers)
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


# --- Endpoints ---


@router.get("/workers/", response_model=list[WorkerSummary])
async def list_workers(request: Request):
    """List all known workers with their status and metadata."""
    redis = request.app.state.redis
    docker = request.app.state.docker
    keys = await redis.keys("worker:status:*")

    workers = []
    for key in keys:
        worker_id = key.split(":", 2)[2]
        status_data = await redis.hgetall(f"worker:status:{worker_id}")
        meta = await redis.hgetall(f"worker:meta:{worker_id}")
        last_activity = await redis.get(f"worker:last_activity:{worker_id}")
        error = await redis.get(f"worker:error:{worker_id}")

        # Cross-check with Docker — override status if container is gone
        redis_status = status_data.get("status", WorkerStatus.UNKNOWN)
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        try:
            await docker.inspect_container(container_name)
        except Exception:
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
                last_activity=last_activity,
                error=error,
            )
        )

    return workers


@router.get("/workers/{worker_id}", response_model=WorkerDetail)
async def get_worker(worker_id: str, request: Request):
    """Get detailed worker info including container details."""
    redis = request.app.state.redis
    docker = request.app.state.docker

    status_data = await _check_worker_exists(redis, worker_id)
    meta = await redis.hgetall(f"worker:meta:{worker_id}")
    last_activity = await redis.get(f"worker:last_activity:{worker_id}")
    error = await redis.get(f"worker:error:{worker_id}")

    container_id = None
    image = None
    redis_status = status_data.get("status", WorkerStatus.UNKNOWN)
    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
    try:
        attrs = await docker.inspect_container(container_name)
        container_id = attrs.get("Id")
        image = attrs.get("Config", {}).get("Image")
    except Exception:
        if redis_status == "RUNNING":
            redis_status = "GONE"

    return WorkerDetail(
        id=worker_id,
        status=redis_status,
        project_id=meta.get("project_id"),
        repo_id=meta.get("repo_id"),
        workspace_path=meta.get("workspace_path"),
        dev_network=meta.get("dev_network"),
        last_activity=last_activity,
        error=error,
        container_id=container_id,
        image=image,
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
