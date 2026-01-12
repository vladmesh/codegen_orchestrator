import json
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

    async def create_worker(self, worker_id: str, image: str, env_vars: Dict[str, str] = None) -> str:
        """
        Create and start a new worker container.
        """
        env_vars = env_vars or {}

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

    async def get_worker_status(self, worker_id: str) -> str:
        """Get status from Redis (primary) or Docker (fallback)."""
        status = await self.redis.get(f"worker:status:{worker_id}")
        if status:
            return status
        return "UNKNOWN"
