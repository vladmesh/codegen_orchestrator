"""Read-only introspection API for admin panel.

Exposes worker status, logs, workspace files, and prompts.
All data comes from Redis metadata + Docker + host filesystem.
"""

import os
from http import HTTPStatus
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api/introspect", tags=["introspect"])

MAX_FILE_SIZE = 1_000_000  # 1 MB
MAX_LOG_TAIL = 5000
DEFAULT_LOG_TAIL = 100


# --- Response models ---


class WorkerSummary(BaseModel):
    id: str
    status: str
    project_id: str | None = None
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


class FileTreeEntry(BaseModel):
    path: str
    is_dir: bool
    size: int


class FileContentResponse(BaseModel):
    worker_id: str
    path: str
    content: str
    size: int


class PromptsResponse(BaseModel):
    worker_id: str
    claude_md: str | None = None
    task_md: str | None = None


# --- Helpers ---


async def _check_worker_exists(redis, worker_id: str) -> dict:
    """Check worker exists in Redis, raise 404 if not."""
    status_data = await redis.hgetall(f"worker:status:{worker_id}")
    if not status_data:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Worker not found")
    return status_data


async def _get_workspace_path(redis, worker_id: str) -> Path:
    """Get workspace path from Redis metadata, raise 404 if not found."""
    meta = await redis.hgetall(f"worker:meta:{worker_id}")
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


def _safe_resolve(workspace: Path, relative_path: str) -> Path:
    """Resolve path safely, raising 403 on traversal attempts."""
    resolved = (workspace / relative_path).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Path traversal not allowed",
        )
    return resolved


# --- Endpoints ---


@router.get("/workers/", response_model=list[WorkerSummary])
async def list_workers(request: Request):
    """List all known workers with their status and metadata."""
    redis = request.app.state.redis
    keys = await redis.keys("worker:status:*")

    workers = []
    for key in keys:
        worker_id = key.split(":", 2)[2]
        status_data = await redis.hgetall(f"worker:status:{worker_id}")
        meta = await redis.hgetall(f"worker:meta:{worker_id}")
        last_activity = await redis.get(f"worker:last_activity:{worker_id}")
        error = await redis.get(f"worker:error:{worker_id}")

        workers.append(
            WorkerSummary(
                id=worker_id,
                status=status_data.get("status", "UNKNOWN"),
                project_id=meta.get("project_id"),
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
    container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
    try:
        attrs = await docker.inspect_container(container_name)
        container_id = attrs.get("Id")
        image = attrs.get("Config", {}).get("Image")
    except Exception:
        pass

    return WorkerDetail(
        id=worker_id,
        status=status_data.get("status", "UNKNOWN"),
        project_id=meta.get("project_id"),
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
    workspace = await _get_workspace_path(redis, worker_id)

    entries = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        rel_dir = Path(dirpath).relative_to(workspace)
        # Add directories (skip root ".")
        if str(rel_dir) != ".":
            entries.append(
                FileTreeEntry(
                    path=str(rel_dir),
                    is_dir=True,
                    size=0,
                )
            )
        # Add files
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(workspace)
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            entries.append(
                FileTreeEntry(
                    path=str(rel),
                    is_dir=False,
                    size=size,
                )
            )

    return entries


@router.get("/workers/{worker_id}/files/{file_path:path}", response_model=FileContentResponse)
async def get_worker_file(worker_id: str, file_path: str, request: Request):
    """Read a file from the worker's workspace."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)
    workspace = await _get_workspace_path(redis, worker_id)

    resolved = _safe_resolve(workspace, file_path)

    if not resolved.exists():
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="File not found")

    if not resolved.is_file():
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Path is not a regular file",
        )

    size = resolved.stat().st_size
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=f"File too large ({size} bytes, max {MAX_FILE_SIZE})",
        )

    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Binary file cannot be read as text",
        )

    return FileContentResponse(
        worker_id=worker_id,
        path=file_path,
        content=content,
        size=size,
    )


@router.get("/workers/{worker_id}/prompts", response_model=PromptsResponse)
async def get_worker_prompts(worker_id: str, request: Request):
    """Read CLAUDE.md and TASK.md from the worker's workspace."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)
    workspace = await _get_workspace_path(redis, worker_id)

    claude_md = None
    task_md = None

    claude_path = workspace / "CLAUDE.md"
    if claude_path.exists():
        try:
            claude_md = claude_path.read_text(encoding="utf-8")
        except Exception:
            pass

    task_path = workspace / "TASK.md"
    if task_path.exists():
        try:
            task_md = task_path.read_text(encoding="utf-8")
        except Exception:
            pass

    return PromptsResponse(worker_id=worker_id, claude_md=claude_md, task_md=task_md)


@router.delete("/workers/{worker_id}", status_code=HTTPStatus.NO_CONTENT)
async def kill_worker(worker_id: str, request: Request):
    """Force-kill a worker container and clean up resources."""
    redis = request.app.state.redis
    await _check_worker_exists(redis, worker_id)

    worker_manager = request.app.state.worker_manager
    await worker_manager.delete_worker(worker_id, reason="admin_kill")
