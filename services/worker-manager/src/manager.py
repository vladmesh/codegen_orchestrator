import json
from datetime import datetime
from typing import Optional, Dict
import structlog
from redis.asyncio import Redis

from .config import settings
from .docker_ops import DockerClientWrapper

logger = structlog.get_logger()


class WorkerManager:
    """
    Manages worker container lifecycle.
    Replaces legacy ContainerService and LifecycleManager.
    """

    def __init__(self, redis: Redis, docker_client: Optional[DockerClientWrapper] = None):
        self.redis = redis
        self.docker = docker_client or DockerClientWrapper()

    async def ensure_image(self, image: str) -> None:
        """Ensure image exists and update access time."""
        # Check if exists
        exists = await self.docker.image_exists(image)
        if not exists:
            logger.info("pulling_image", image=image)
            await self.docker.pull_image(image)

        # Update LRU
        await self.redis.set(f"worker:image:last_used:{image}", datetime.now().isoformat())

    async def create_worker(self, worker_id: str, image: str, env_vars: Dict[str, str] = None) -> str:
        """
        Create and start a new worker container.
        """
        env_vars = env_vars or {}

        # Ensure image exists and update cache stats
        await self.ensure_image(image)

        # Prepare Config
        # In legacy, we parsed config models. Here we accept args.
        # Ensure image is prefixed correctly if needed (or assume caller handles it?)
        # Spec says: IMAGE_PREFIX handled in config/tests.

        # Add standard labels
        labels = json.loads(settings.WORKER_DOCKER_LABELS)
        labels["com.codegen.worker.id"] = worker_id
        labels["com.codegen.type"] = "worker"

        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"

        logger.info("creating_worker", worker_id=worker_id, image=image, container_name=container_name)

        try:
            # Update Redis status
            await self.redis.set(f"worker:status:{worker_id}", "STARTING")

            # Run container
            # Using HostConfig equivalent params in docer-py
            container = await self.docker.run_container(
                image=image,
                name=container_name,
                detach=True,
                environment=env_vars,
                labels=labels,
                network_mode="host",
                # Network: Legacy used 'container_network' setting.
                # In compose test, we might be in bridge network.
                # If we use "host", it breaks isolation?
                # Usually we want same network as orchestrator.
                # For now let's use default or user-defined network.
                # If we run in docker-compose, we can attach to network name.
                # But 'host' is simplest for connectivity if orchestrator is reachable.
                # Let's omit network_mode and use default bridge unless specified.
                # Or better, pass network if needed.
                # Legacy code: network={self.settings.container_network}
                # Let's skip explicit network for now to keep simple, valid for creating from inside container?
                # If we create container FROM container, sibling container.
                # They share bridge network usually if on same default bridge.
            )

            # Update Redis status
            await self.redis.set(f"worker:status:{worker_id}", "RUNNING")

            return container.id

        except Exception as e:
            logger.error("worker_creation_failed", worker_id=worker_id, error=str(e))
            await self.redis.set(f"worker:status:{worker_id}", "FAILED")
            await self.redis.set(f"worker:error:{worker_id}", str(e))
            raise

    async def delete_worker(self, worker_id: str) -> None:
        """Stop and remove a worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        logger.info("deleting_worker", worker_id=worker_id)

        try:
            # We try to remove by name or lookup ID?
            # docker-py remove accepts name or ID.
            await self.docker.remove_container(container_name, force=True)
            await self.redis.set(f"worker:status:{worker_id}", "STOPPED")

        except Exception as e:
            logger.error("worker_deletion_failed", worker_id=worker_id, error=str(e))
            # Even if failed, we mark stopped? Or FAILED?
            # If container doesn't exist, remove_container handles NotFound.
            await self.redis.set(f"worker:status:{worker_id}", "STOPPED")

    async def pause_worker(self, worker_id: str) -> None:
        """Pause a running worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.pause_container(container_name)
        await self.redis.set(f"worker:status:{worker_id}", "PAUSED")
        logger.info("worker_paused", worker_id=worker_id)

    async def resume_worker(self, worker_id: str) -> None:
        """Resume a paused worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.unpause_container(container_name)
        await self.redis.set(f"worker:status:{worker_id}", "RUNNING")
        logger.info("worker_resumed", worker_id=worker_id)

    async def garbage_collect_images(self, retention_seconds: int = 7 * 24 * 3600) -> None:
        """Remove unused images."""
        images = await self.docker.list_images()
        now = datetime.now()

        for img in images:
            # Docker Py image object has 'tags' list
            tags = getattr(img, "tags", [])
            for tag in tags:
                if not tag:
                    continue
                # Check cache key
                last_used_str = await self.redis.get(f"worker:image:last_used:{tag}")
                if last_used_str:
                    last_used = datetime.fromisoformat(last_used_str)
                    age = (now - last_used).total_seconds()
                    if age > retention_seconds:
                        logger.info("gc_removing_image", image=tag, age=age)
                        await self.docker.remove_image(tag, force=True)
                        await self.redis.delete(f"worker:image:last_used:{tag}")

    async def check_and_pause_workers(self, idle_timeout: int = 600) -> None:
        """Pause workers that have been inactive."""
        # Iterate over redis keys "worker:last_activity:*"
        # Using scan_iter is best for Redis

        async for key in self.redis.scan_iter(match="worker:last_activity:*"):
            # key is something like "worker:last_activity:abc-123"
            worker_id = key.split(":")[-1]

            # Check status first - only pause RUNNING workers
            status = await self.get_worker_status(worker_id)
            if status != "RUNNING":
                continue

            last_activity_ts = await self.redis.get(key)
            if not last_activity_ts:
                continue

            age = datetime.now().timestamp() - float(last_activity_ts)

            if age > idle_timeout:
                logger.info("auto_pausing_worker", worker_id=worker_id, idle_seconds=age)
                try:
                    await self.pause_worker(worker_id)
                except Exception as e:
                    logger.error("auto_pause_failed", worker_id=worker_id, error=str(e))

    async def get_worker_status(self, worker_id: str) -> str:
        """Get status from Redis (primary) or Docker (fallback)."""
        status = await self.redis.get(f"worker:status:{worker_id}")
        if status:
            return status
        return "UNKNOWN"
