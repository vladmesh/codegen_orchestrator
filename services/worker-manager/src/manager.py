import json
import os
from datetime import datetime
from typing import Optional, Dict, List
import structlog
from redis.asyncio import Redis

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import ImageBuilder
from .container_config import WorkerContainerConfig
from . import workspace as workspace_mod
from .compose_runner import ComposeRunner

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
        create_dev_network: bool = True,
        workspace_path: Optional[str] = None,
    ) -> str:
        """
        Create and start a new worker container.

        Args:
            network_name: Primary Docker network to attach to. If None, uses host networking.
            create_dev_network: If True, also create a dev_proj_<worker_id> network and
                                connect the container to it as a second network.
            workspace_path: Host path to the worker workspace (stored in Redis metadata).
        """
        env_vars = env_vars or {}

        # Ensure image exists and update cache stats
        await self.ensure_image(image)

        # Add standard labels
        labels = json.loads(settings.WORKER_DOCKER_LABELS)
        labels["com.codegen.worker.id"] = worker_id
        labels["com.codegen.type"] = "worker"

        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        dev_network = f"dev_proj_{worker_id}"

        logger.info(
            "creating_worker",
            worker_id=worker_id,
            image=image,
            container_name=container_name,
            network=network_name or "host",
            dev_network=dev_network if create_dev_network else None,
        )

        try:
            # Remove stale container with the same name (if any)
            await self.docker.remove_container(container_name, force=True)

            # Create dev network before starting container
            if create_dev_network:
                await self.docker.create_network(dev_network)

            # Update Redis status
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "STARTING"})

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

            # Connect to dev network as second network
            if create_dev_network:
                await self.docker.connect_network(dev_network, container.id)

            # Persist metadata in Redis
            meta: Dict[str, str] = {"dev_network": dev_network}
            if workspace_path:
                meta["workspace_path"] = workspace_path
            await self.redis.hset(f"worker:meta:{worker_id}", mapping=meta)

            # Update Redis status
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})

            return container.id

        except Exception as e:
            logger.error("worker_creation_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "FAILED"})
            await self.redis.set(f"worker:error:{worker_id}", str(e))
            raise

    async def delete_worker(self, worker_id: str) -> None:
        """Stop and remove a worker, its dev network, workspace, and Redis keys."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        logger.info("deleting_worker", worker_id=worker_id)

        # Retrieve metadata stored at creation time
        meta = await self.redis.hgetall(f"worker:meta:{worker_id}")
        dev_network = meta.get("dev_network") if meta else None
        stored_workspace = meta.get("workspace_path") if meta else None

        try:
            # Tear down sidecar containers launched via compose before removing worker
            if stored_workspace:
                try:
                    runner = ComposeRunner(settings.WORKSPACE_BASE_PATH)
                    exit_code, stdout, stderr = await runner.run(worker_id, ["down", "-v"], timeout=60)
                    if exit_code != 0:
                        logger.warning(
                            "compose_down_nonzero",
                            worker_id=worker_id,
                            exit_code=exit_code,
                            stderr=stderr,
                        )
                except Exception as e:
                    logger.warning("compose_down_failed", worker_id=worker_id, error=str(e))

            # Remove worker container
            await self.docker.remove_container(container_name, force=True)

            # Remove dev network
            if dev_network:
                await self.docker.remove_network(dev_network)

            # Remove workspace directory from host
            workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, worker_id)

            # Clean up all Redis keys for this worker
            keys_to_delete = [
                f"worker:status:{worker_id}",
                f"worker:meta:{worker_id}",
                f"worker:error:{worker_id}",
                f"worker:last_activity:{worker_id}",
            ]
            await self.redis.delete(*keys_to_delete)

        except Exception as e:
            logger.error("worker_deletion_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "STOPPED"})

    async def pause_worker(self, worker_id: str) -> None:
        """Pause a running worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.pause_container(container_name)
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "PAUSED"})
        logger.info("worker_paused", worker_id=worker_id)

    async def resume_worker(self, worker_id: str) -> None:
        """Resume a paused worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.unpause_container(container_name)
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
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
        status = await self.redis.hget(f"worker:status:{worker_id}", "status")
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
        task_content: str | None = None,
        # Auth config
        auth_mode: str = "host_session",
        host_claude_dir: str | None = None,
        api_key: str | None = None,
        env_vars: Dict[str, str] = None,
        worker_type: str = "developer",
    ) -> str:
        """
        Create worker with specified capabilities and agent config.
        Injects instructions (-> instruction file) and task_content (-> TASK.md) if provided.
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
            worker_type=worker_type,
            agent_type=agent_type,
            capabilities=capabilities,
            auth_mode=auth_mode,
            host_claude_dir=host_claude_dir,
            api_key=api_key,
        )

        # Create workspace on the host
        ws_path = workspace_mod.create_workspace(settings.WORKSPACE_BASE_PATH, worker_id)
        config.workspace_host_path = workspace_mod.get_workspace_host_path(settings.WORKSPACE_BASE_PATH, worker_id)

        # Generate container params
        # Use bridge-network URLs (services reachable via Docker DNS in bridge mode)
        worker_redis_url = "redis://redis:6379"
        worker_api_url = "http://api:8000"
        container_env = config.to_env_vars(
            redis_url=worker_redis_url,
            api_url=worker_api_url,
            subprocess_timeout_seconds=settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS,
            worker_manager_url=settings.WORKER_MANAGER_URL,
        )
        container_env.update(env_vars)

        # Propagate GH_TOKEN for GitHub CLI (gh) authentication
        github_token = env_vars.get("GITHUB_TOKEN")
        if github_token:
            container_env["GH_TOKEN"] = github_token

        # Propagate secrets encryption key for orchestrator-cli
        secrets_key = os.getenv("SECRETS_ENCRYPTION_KEY")
        if secrets_key:
            container_env["SECRETS_ENCRYPTION_KEY"] = secrets_key

        # Volumes
        volumes = config.to_volume_mounts()

        # Always use INTERNAL_NETWORK for the primary network
        network_name = settings.INTERNAL_NETWORK

        # Create container with dual-network setup
        container_id = await self.create_worker(
            worker_id=worker_id,
            image=image_tag,
            env_vars=container_env,
            volumes=volumes,
            network_name=network_name,
            create_dev_network=True,
            workspace_path=str(ws_path),
        )

        # Auto-setup git repository FIRST (before instructions)
        # This is important because instructions go to /workspace/CLAUDE.md
        # and git clone requires empty directory
        repo_name = env_vars.get("REPO_NAME")
        github_token = env_vars.get("GITHUB_TOKEN")
        if repo_name and github_token:
            await self._setup_git_repo(container_id, repo_name, github_token, worker_id)

        # Inject instructions AFTER git clone (so instruction file doesn't block clone)
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
                container_logs = await self.docker.get_container_logs(container_id)
                logger.error(
                    "instruction_injection_failed",
                    worker_id=worker_id,
                    error=output,
                    container_logs=container_logs,
                )

        # Inject task content as TASK.md (for task-driven workers like developer)
        if task_content:
            task_path = "/home/worker/TASK.md"
            logger.info("injecting_task_content", worker_id=worker_id, path=task_path)

            import base64

            encoded_task = base64.b64encode(task_content.encode()).decode()
            cmd = f"python3 -c \"import base64; open('{task_path}', 'w').write(base64.b64decode('{encoded_task}').decode())\""

            exit_code, output = await self.docker.exec_in_container(container_id, cmd)
            if exit_code != 0:
                container_logs = await self.docker.get_container_logs(container_id)
                logger.error(
                    "task_injection_failed",
                    worker_id=worker_id,
                    error=output,
                    container_logs=container_logs,
                )

        # Return the worker_id (name), not container_id (Docker hash)
        # This allows callers to reference the worker by its logical name
        return worker_id

    async def _setup_git_repo(
        self,
        container_id: str,
        repo: str,
        token: str,
        worker_id: str,
    ) -> bool:
        """Clone repository and configure git hooks before LLM starts.

        This saves tokens by automating:
        - git clone
        - git config core.hooksPath (enables pre-commit/pre-push hooks)
        - git user config

        Args:
            container_id: Docker container ID
            repo: Repository in "owner/repo" format
            token: GitHub access token
            worker_id: Worker ID for logging

        Returns:
            True if setup succeeded, False otherwise
        """
        logger.info("git_repo_setup_start", worker_id=worker_id, repo=repo)

        # Clone repo and configure git in one script
        # Using x-access-token for GitHub App token authentication
        setup_script = f"""set -e
cd /workspace
git clone "https://x-access-token:{token}@github.com/{repo}" .
git config core.hooksPath .githooks
git config user.name "AI Agent"
git config user.email "ai@codegen.local"
"""

        # Use base64 encoding to avoid shell quoting issues (same pattern as instructions)
        import base64

        encoded = base64.b64encode(setup_script.encode()).decode()
        # Wrap in bash -c because docker exec_run doesn't use shell by default
        cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"

        exit_code, output = await self.docker.exec_in_container(
            container_id,
            cmd,
            timeout=120,  # git clone may take time
        )

        if exit_code != 0:
            logger.error(
                "git_repo_setup_failed",
                worker_id=worker_id,
                repo=repo,
                exit_code=exit_code,
                error=output,
            )
            return False

        logger.info("git_repo_setup_complete", worker_id=worker_id, repo=repo)
        return True
