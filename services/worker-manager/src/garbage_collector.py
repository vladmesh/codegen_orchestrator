"""Garbage collection for orphaned containers, networks, workspaces, and images."""

import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus
from shared.redis import decode_redis_fields

from .config import settings, worker_urls
from .docker_ops import DockerClientWrapper
from . import workspace as workspace_mod

logger = structlog.get_logger()

# Statuses that indicate the worker is no longer alive and can be cleaned up
_TERMINAL_STATUSES = frozenset({WorkerStatus.DEAD, WorkerStatus.FAILED, WorkerStatus.STOPPED})


async def garbage_collect_orphaned_resources(redis: Redis, docker: DockerClientWrapper, *, delete_worker_fn) -> None:
    """Find and remove orphaned containers, networks, and workspaces.

    After a crash/OOM, resources may be left behind without corresponding
    Redis state. This method collects the set of known worker IDs from Redis
    and removes any Docker containers, dev networks, or workspace directories
    that don't belong to a known worker.

    Order: containers -> networks -> workspaces (networks can't be removed
    while containers are connected).
    """
    # Collect known worker IDs from Redis
    known_ids: set[str] = set()
    async for key in redis.scan_iter(match="worker:status:*"):
        known_ids.add(key.split(":")[-1])

    logger.info("orphan_gc_start", known_workers=len(known_ids))

    # --- Orphaned containers ---
    try:
        containers = await docker.list_containers(filters={"label": "com.codegen.type=worker"}, all=True)
    except Exception as e:
        logger.error("orphan_gc_list_containers_failed", error=str(e))
        containers = []

    # Collect live container IDs for reverse check
    live_container_ids: set[str] = set()
    for container in containers:
        worker_id = container.labels.get("com.codegen.worker.id")
        if worker_id and worker_id not in known_ids:
            logger.info("orphan_gc_removing_container", worker_id=worker_id)
            try:
                await delete_worker_fn(worker_id)
            except Exception as e:
                logger.error("orphan_gc_delete_worker_failed", worker_id=worker_id, error=str(e))
        elif worker_id:
            live_container_ids.add(worker_id)

    # --- Stale Redis entries (Redis says alive, but no container) ---
    for worker_id in known_ids:
        if worker_id not in live_container_ids:
            status = await redis.hget(f"worker:status:{worker_id}", "status")
            if status and status not in _TERMINAL_STATUSES:
                logger.warning(
                    "orphan_gc_stale_redis",
                    worker_id=worker_id,
                    redis_status=status,
                )
                try:
                    await delete_worker_fn(worker_id)
                except Exception as e:
                    logger.error(
                        "orphan_gc_stale_cleanup_failed",
                        worker_id=worker_id,
                        error=str(e),
                    )

    # --- Orphaned networks ---
    try:
        networks = await docker.list_networks()
    except Exception as e:
        logger.error("orphan_gc_list_networks_failed", error=str(e))
        networks = []

    for network in networks:
        name = network.name
        if name.startswith("dev_proj_"):
            worker_id = name[len("dev_proj_") :]
            if worker_id not in known_ids:
                logger.info("orphan_gc_removing_network", network=name, worker_id=worker_id)
                try:
                    await docker.remove_network(name)
                except Exception as e:
                    logger.error("orphan_gc_remove_network_failed", network=name, error=str(e))

    logger.info("orphan_gc_complete")


async def garbage_collect_workspaces(redis: Redis, *, max_age_hours: int = 35) -> None:
    """Remove project workspaces older than max_age_hours with no active workers.

    Scans SCAFFOLDED_WORKSPACE_PATH for old workspaces. Also cleans
    stale workspace:active_projects entries.
    """
    # Clean stale active_projects entries — remove projects with no live worker
    active_projects = await redis.smembers("workspace:active_projects")
    for project_id in active_projects:
        has_worker = False
        async for key in redis.scan_iter(match="worker:meta:*"):
            meta = decode_redis_fields(await redis.hgetall(key))
            if meta.get("project_id") == project_id:
                has_worker = True
                break
        if not has_worker:
            await redis.srem("workspace:active_projects", project_id)
            logger.info("workspace_gc_cleared_stale_project", project_id=project_id)

    # Refresh after cleanup
    active_projects = await redis.smembers("workspace:active_projects")

    now = time.time()
    for base_path in [settings.SCAFFOLDED_WORKSPACE_PATH]:
        try:
            entries = os.listdir(base_path)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.error("workspace_gc_list_failed", base_path=base_path, error=str(e))
            continue

        for entry in entries:
            if entry in active_projects:
                continue
            ws_dir = Path(base_path) / entry
            try:
                age_hours = (now - ws_dir.stat().st_mtime) / 3600
            except OSError:
                continue
            if age_hours > max_age_hours:
                workspace_mod.remove_workspace(base_path, entry)
                await _notify_workspace_deleted(entry)
                logger.info(
                    "workspace_gc_removed",
                    project_id=entry,
                    base_path=base_path,
                    age_hours=round(age_hours, 1),
                )


async def _notify_workspace_deleted(repo_id: str) -> None:
    """Notify API that a workspace was GC'd so workspace_ready is cleared."""
    _, api_url = worker_urls(settings)
    url = f"{api_url}/api/repositories/{repo_id}/notify-workspace-deleted"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url)
        if resp.status_code == 200:
            logger.info("workspace_gc_notified_api", repo_id=repo_id)
        else:
            logger.warning(
                "workspace_gc_notify_failed",
                repo_id=repo_id,
                status=resp.status_code,
            )
    except Exception as exc:
        logger.warning("workspace_gc_notify_error", repo_id=repo_id, error=str(exc))


async def garbage_collect_images(
    redis: Redis, docker: DockerClientWrapper, *, retention_seconds: int = 7 * 24 * 3600
) -> None:
    """Remove unused images."""
    images = await docker.list_images()
    now = datetime.now()

    for img in images:
        tags = getattr(img, "tags", [])
        for tag in tags:
            if not tag:
                continue
            last_used_str = await redis.get(f"worker:image:last_used:{tag}")
            if last_used_str:
                last_used = datetime.fromisoformat(last_used_str)
                age = (now - last_used).total_seconds()
                if age > retention_seconds:
                    logger.info("gc_removing_image", image=tag, age=age)
                    await docker.remove_image(tag, force=True)
                    await redis.delete(f"worker:image:last_used:{tag}")
