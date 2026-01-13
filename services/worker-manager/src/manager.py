import json
from datetime import datetime
from typing import Optional, Dict, List
import structlog
from redis.asyncio import Redis

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import ImageBuilder
from .container_config import WorkerContainerConfig

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

    async def create_worker(
        self,
        worker_id: str,
        image: str,
        env_vars: Dict[str, str] = None,
        volumes: Dict[str, Dict[str, str]] = None,
        network_name: Optional[str] = None,
    ) -> str:
        """
        Create and start a new worker container.

        Args:
            network_name: Docker network to attach to. If None, uses host networking.
        """
        env_vars = env_vars or {}

        # Ensure image exists and update cache stats
        await self.ensure_image(image)

        # Add standard labels
        labels = json.loads(settings.WORKER_DOCKER_LABELS)
        labels["com.codegen.worker.id"] = worker_id
        labels["com.codegen.type"] = "worker"

        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"

        logger.info(
            "creating_worker",
            worker_id=worker_id,
            image=image,
            container_name=container_name,
            network=network_name or "host",
        )

        try:
            # Update Redis status
            await self.redis.set(f"worker:status:{worker_id}", "STARTING")

            # Build run kwargs
            run_kwargs = {
                "image": image,
                "name": container_name,
                "detach": True,
                "environment": env_vars,
                "labels": labels,
                "volumes": volumes,
            }

            # Network configuration
            if network_name:
                run_kwargs["network"] = network_name
            else:
                run_kwargs["network_mode"] = "host"

            container = await self.docker.run_container(**run_kwargs)

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

    async def ensure_or_build_image(
        self,
        capabilities: List[str],
        base_image: str,
        prefix: str,
        agent_type: str = "claude",
    ) -> str:
        """
        Ensure image with given capabilities exists, building if necessary.

        Args:
            capabilities: List of capabilities (e.g., ["GIT", "CURL"])
            base_image: Base image to extend (e.g., "worker-base:latest")
            prefix: Image name prefix (e.g., "worker" or "worker-test")
            agent_type: Agent type ("claude" or "factory")

        Returns:
            Full image tag (e.g., "worker:abc123def456")
        """
        builder = ImageBuilder(base_image=base_image)
        image_tag = builder.get_image_tag(capabilities=capabilities, prefix=prefix, agent_type=agent_type)

        # Check cache
        exists = await self.docker.image_exists(image_tag)

        if not exists:
            # Cache miss - need to build
            logger.info(
                "image_cache_miss",
                image_tag=image_tag,
                capabilities=capabilities,
                agent_type=agent_type,
            )
            dockerfile = builder.generate_dockerfile(capabilities=capabilities, agent_type=agent_type)
            await self.docker.build_image(
                dockerfile_content=dockerfile,
                tag=image_tag,
            )
            logger.info("image_built", image_tag=image_tag)
        else:
            logger.info("image_cache_hit", image_tag=image_tag)

        # Update LRU timestamp
        await self.redis.set(
            f"worker:image:last_used:{image_tag}",
            datetime.now().isoformat(),
        )

        return image_tag

    def _get_agent(self, agent_type: str):
        """Get agent instance by type."""
        from .agents import ClaudeCodeAgent, FactoryDroidAgent

        if agent_type == "factory":
            return FactoryDroidAgent()
        return ClaudeCodeAgent()

    async def create_worker_with_capabilities(
        self,
        worker_id: str,
        capabilities: List[str],
        base_image: str,
        agent_type: str = "claude",
        prefix: str | None = None,
        instructions: str | None = None,
        # Auth config
        auth_mode: str = "host_session",
        host_claude_dir: str | None = None,
        api_key: str | None = None,
        env_vars: Dict[str, str] = None,
    ) -> str:
        """
        Create worker with specified capabilities and agent config.
        Injects instructions if provided.
        """
        prefix = prefix or settings.WORKER_IMAGE_PREFIX
        env_vars = env_vars or {}

        # Ensure image exists
        image_tag = await self.ensure_or_build_image(
            capabilities=capabilities,
            base_image=base_image,
            prefix=prefix,
            agent_type=agent_type,
        )

        # Get Agent Logic
        agent = self._get_agent(agent_type)

        # Create Config
        config = WorkerContainerConfig(
            worker_id=worker_id,
            worker_type="developer",
            agent_type=agent_type,
            capabilities=capabilities,
            auth_mode=auth_mode,
            host_claude_dir=host_claude_dir,
            api_key=api_key,
        )

        # Generate container params
        # Env vars - use WORKER_* URLs if set (for DIND where DNS doesn't work)
        worker_redis_url = settings.WORKER_REDIS_URL or settings.REDIS_URL
        worker_api_url = settings.WORKER_API_URL or "http://api:8000"
        container_env = config.to_env_vars(
            redis_url=worker_redis_url,
            api_url=worker_api_url,
        )
        container_env.update(env_vars)

        # Volumes
        volumes = config.to_volume_mounts()

        # Network: use DOCKER_NETWORK from settings if set, else None (host mode)
        network_name = settings.DOCKER_NETWORK if settings.DOCKER_NETWORK else None

        # Create container
        container_id = await self.create_worker(
            worker_id=worker_id,
            image=image_tag,
            env_vars=container_env,
            volumes=volumes,
            network_name=network_name,
        )

        # Inject instructions if provided
        if instructions:
            target_path = agent.get_instruction_path()
            logger.info("injecting_instructions", worker_id=worker_id, path=target_path)

            # Use base64 encoding to avoid shell quoting issues
            # Worker base has python3 installed
            import base64

            encoded = base64.b64encode(instructions.encode()).decode()

            # Python one-liner to decode base64 and write to file
            cmd = f"python3 -c \"import base64; open('{target_path}', 'w').write(base64.b64decode('{encoded}').decode())\""

            exit_code, output = await self.docker.exec_in_container(container_id, cmd)
            if exit_code != 0:
                logger.error("instruction_injection_failed", worker_id=worker_id, error=output)

        # Return the worker_id (name), not container_id (Docker hash)
        # This allows callers to reference the worker by its logical name
        return worker_id
