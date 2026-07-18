import base64
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, List

import structlog
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.vocab import AgentType
from shared.redis import decode_redis_fields

from .config import settings, worker_urls
from .docker_ops import DockerClientWrapper
from .image_builder import ImageBuilder
from .container_config import WorkerContainerConfig
from . import workspace as workspace_mod
from .compose_runner import ComposeRunner
from . import garbage_collector as gc
from . import git_ops

if TYPE_CHECKING:
    from shared.contracts.queues.worker import ScaffoldConfig

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
        exists = await self.docker.image_exists(image)
        if not exists:
            logger.info("pulling_image", image=image)
            await self.docker.pull_image(image)

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

        await self.ensure_image(image)

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
            await self.docker.remove_container(container_name, force=True)

            if create_dev_network:
                await self.docker.create_network(dev_network)

            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STARTING})

            run_kwargs = {
                "image": image,
                "name": container_name,
                "detach": True,
                "environment": env_vars,
                "labels": labels,
                "volumes": volumes,
            }

            if network_name:
                run_kwargs["network"] = network_name
            else:
                run_kwargs["network_mode"] = "host"

            container = await self.docker.run_container(**run_kwargs)

            if create_dev_network:
                await self.docker.connect_network(dev_network, container.id)

            meta: Dict[str, str] = {"dev_network": dev_network}
            if workspace_path:
                meta["workspace_path"] = workspace_path
            await self.redis.hset(f"worker:meta:{worker_id}", mapping=meta)

            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})

            return container.id

        except Exception as e:
            logger.error("worker_creation_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})
            await self.redis.set(f"worker:error:{worker_id}", str(e))
            raise

    async def delete_worker(self, worker_id: str, reason: str | None = None) -> None:
        """Stop and remove a worker, its dev network, workspace, and Redis keys."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        logger.info("deleting_worker", worker_id=worker_id)

        meta = decode_redis_fields(await self.redis.hgetall(f"worker:meta:{worker_id}"))
        dev_network = meta.get("dev_network") if meta else None
        stored_workspace = meta.get("workspace_path") if meta else None
        project_id = meta.get("project_id") if meta else None

        try:
            if stored_workspace:
                try:
                    runner = ComposeRunner(settings.SCAFFOLDED_WORKSPACE_PATH)
                    exit_code, stdout, stderr = await runner.run(
                        worker_id,
                        ["down", "-v"],
                        timeout=60,
                        workspace_dir=stored_workspace,
                    )
                    if exit_code != 0:
                        logger.warning(
                            "compose_down_nonzero",
                            worker_id=worker_id,
                            exit_code=exit_code,
                            stderr=stderr,
                        )
                except Exception as e:
                    logger.warning("compose_down_failed", worker_id=worker_id, error=str(e))

            await self.docker.remove_container(container_name, force=True)

            if dev_network:
                await self.docker.remove_network(dev_network)

        except Exception as e:
            logger.error("worker_deletion_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STOPPED})
        finally:
            if project_id:
                logger.info("workspace_preserved", project_id=project_id, worker_id=worker_id)
                await self.redis.srem("workspace:active_projects", project_id)

                if reason:
                    failure_key = f"workspace:{project_id}:failure_count"
                    if reason in ("failed", "timeout"):
                        await self.redis.incr(failure_key)
                        await self.redis.expire(failure_key, 48 * 3600)
                    elif reason == "completed":
                        await self.redis.delete(failure_key)

            keys_to_delete = [
                f"worker:status:{worker_id}",
                f"worker:meta:{worker_id}",
                f"worker:error:{worker_id}",
                f"worker:last_activity:{worker_id}",
                f"worker:{worker_id}:input",
                f"worker:{worker_id}:output",
            ]
            await self.redis.delete(*keys_to_delete)

    async def pause_worker(self, worker_id: str) -> None:
        """Pause a running worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.pause_container(container_name)
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.PAUSED})
        logger.info("worker_paused", worker_id=worker_id)

    async def resume_worker(self, worker_id: str) -> None:
        """Resume a paused worker."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        await self.docker.unpause_container(container_name)
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})
        logger.info("worker_resumed", worker_id=worker_id)

    # --- Garbage collection (delegated to garbage_collector module) ---

    async def garbage_collect_orphaned_resources(self) -> None:
        """Find and remove orphaned containers, networks, and workspaces."""
        await gc.garbage_collect_orphaned_resources(self.redis, self.docker, delete_worker_fn=self.delete_worker)

    async def garbage_collect_workspaces(self, max_age_hours: int = 35) -> None:
        """Remove project workspaces older than max_age_hours with no active workers."""
        await gc.garbage_collect_workspaces(self.redis, max_age_hours=max_age_hours)

    async def garbage_collect_images(self, retention_seconds: int = 7 * 24 * 3600) -> None:
        """Remove unused images."""
        await gc.garbage_collect_images(self.redis, self.docker, retention_seconds=retention_seconds)

    # --- Worker idle management ---

    async def check_and_pause_workers(self, idle_timeout: int = 600) -> None:
        """Pause workers that have been inactive."""
        async for key in self.redis.scan_iter(match="worker:last_activity:*"):
            worker_id = key.split(":")[-1]

            status = await self.get_worker_status(worker_id)
            if status != WorkerStatus.RUNNING:
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
        return WorkerStatus.UNKNOWN

    # --- Image management ---

    async def ensure_or_build_image(
        self,
        capabilities: List[str],
        base_image: str,
        prefix: str,
        agent_type: AgentType = AgentType.CLAUDE,
    ) -> str:
        """
        Ensure image with given capabilities exists, building if necessary.

        Returns:
            Full image tag (e.g., "worker:abc123def456")
        """
        builder = ImageBuilder(base_image=base_image)
        image_tag = builder.get_image_tag(capabilities=capabilities, prefix=prefix, agent_type=agent_type)

        exists = await self.docker.image_exists(image_tag)

        if not exists:
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

        await self.redis.set(
            f"worker:image:last_used:{image_tag}",
            datetime.now().isoformat(),
        )

        return image_tag

    def _get_agent(self, agent_type: AgentType):
        """Get agent instance by type."""
        from .agents import ClaudeCodeAgent, CodexAgent, FactoryDroidAgent

        if agent_type == AgentType.FACTORY:
            return FactoryDroidAgent()
        if agent_type == AgentType.CODEX:
            return CodexAgent()
        if agent_type in {AgentType.CLAUDE, AgentType.NOOP}:
            return ClaudeCodeAgent()
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Statuses that indicate the worker is no longer alive and can be cleaned up
    _TERMINAL_STATUSES = frozenset({WorkerStatus.DEAD, WorkerStatus.FAILED, WorkerStatus.STOPPED})

    async def _check_project_lock(self, project_id: str) -> str | None:
        """Check if another worker is active for this project.

        Returns worker_id if locked, None if free.
        Auto-cleans stale Redis keys for workers in terminal states (DEAD/FAILED/STOPPED).
        """
        if not await self.redis.sismember("workspace:active_projects", project_id):
            return None
        async for key in self.redis.scan_iter(match="worker:meta:*"):
            meta = decode_redis_fields(await self.redis.hgetall(key))
            if meta.get("project_id") == project_id:
                worker_id = key.split(":")[-1]
                status = await self.redis.hget(f"worker:status:{worker_id}", "status")
                if status in self._TERMINAL_STATUSES:
                    logger.warning(
                        "stale_worker_auto_cleanup",
                        worker_id=worker_id,
                        project_id=project_id,
                        status=status,
                    )
                    await self.redis.delete(
                        f"worker:status:{worker_id}",
                        f"worker:meta:{worker_id}",
                        f"worker:error:{worker_id}",
                        f"worker:last_activity:{worker_id}",
                    )
                    await self.redis.srem("workspace:active_projects", project_id)
                    return None
                return worker_id
        return None

    async def create_worker_with_capabilities(
        self,
        worker_id: str,
        capabilities: List[str],
        base_image: str,
        agent_type: AgentType = AgentType.CLAUDE,
        prefix: str | None = None,
        instructions: str | None = None,
        task_content: str | None = None,
        auth_mode: str = "host_session",
        host_claude_dir: str | None = None,
        host_codex_home: str | None = None,
        api_key: str | None = None,
        env_vars: Dict[str, str] = None,
        worker_type: str = "developer",
        project_id: str | None = None,
        repo_id: str | None = None,
        scaffold_config: "ScaffoldConfig | None" = None,
        branch: str | None = None,
    ) -> str:
        """
        Create worker with specified capabilities and agent config.
        Injects instructions (-> instruction file) and task_content (-> TASK.md) if provided.
        """
        logger.info(
            "create_worker_with_capabilities",
            worker_id=worker_id,
            project_id=project_id,
        )

        if agent_type == AgentType.CODEX and auth_mode == "host_session":
            from .codex_auth import validate_codex_host_session

            validation_path = settings.HOST_CODEX_VALIDATION_PATH or host_codex_home
            validate_codex_host_session(validation_path)

        if project_id:
            existing_worker = await self._check_project_lock(project_id)
            if existing_worker:
                raise RuntimeError(f"Project {project_id} already has active worker {existing_worker}")

            failure_key = f"workspace:{project_id}:failure_count"
            failure_count = int(await self.redis.get(failure_key) or 0)

            if failure_count >= 3:
                raise RuntimeError(f"Max retries (3) exceeded for project {project_id}. Reset with: DEL {failure_key}")

            # Register project lock early so spawner gets worker_id before image build
            await self.redis.hset(f"worker:meta:{worker_id}", "project_id", project_id)
            await self.redis.sadd("workspace:active_projects", project_id)
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.BUILDING})

        prefix = prefix or settings.WORKER_IMAGE_PREFIX
        env_vars = env_vars or {}

        try:
            image_tag = await self.ensure_or_build_image(
                capabilities=capabilities,
                base_image=base_image,
                prefix=prefix,
                agent_type=agent_type,
            )

            agent = self._get_agent(agent_type)

            config = WorkerContainerConfig(
                worker_id=worker_id,
                worker_type=worker_type,
                agent_type=agent_type,
                capabilities=capabilities,
                auth_mode=auth_mode,
                host_claude_dir=host_claude_dir,
                host_codex_home=host_codex_home,
                api_key=api_key,
            )

            if not repo_id:
                raise RuntimeError(
                    "repo_id is required — all workers must use pre-scaffolded workspaces. "
                    "Ensure scaffolder has run before spawning workers."
                )
            ws_path, scaffolded_exists = workspace_mod.get_scaffolded_workspace(
                settings.SCAFFOLDED_WORKSPACE_PATH, repo_id
            )
            if not scaffolded_exists:
                raise RuntimeError(
                    f"Scaffolded workspace not found for repo_id={repo_id} at {ws_path}. Scaffolder must run first."
                )
            config.workspace_host_path = str(ws_path)
            logger.info(
                "using_scaffolded_workspace",
                worker_id=worker_id,
                repo_id=repo_id,
                path=str(ws_path),
            )

            worker_redis_url, worker_api_url = worker_urls(settings)
            container_env = config.to_env_vars(
                redis_url=worker_redis_url,
                api_url=worker_api_url,
                subprocess_timeout_seconds=settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS,
                worker_manager_url=settings.WORKER_MANAGER_URL,
            )
            container_env.update(env_vars)
            if agent_type == AgentType.FACTORY and "FACTORY_API_KEY" not in container_env:
                factory_api_key = os.getenv("FACTORY_API_KEY")
                if not factory_api_key:
                    raise RuntimeError("FACTORY_API_KEY is not set")
                container_env["FACTORY_API_KEY"] = factory_api_key

            github_token = env_vars.get("GITHUB_TOKEN")
            if github_token:
                container_env["GH_TOKEN"] = github_token

            secrets_key = os.getenv("SECRETS_ENCRYPTION_KEY")
            if secrets_key:
                container_env["SECRETS_ENCRYPTION_KEY"] = secrets_key

            volumes = config.to_volume_mounts()

            if settings.DOCKER_NETWORK:
                network_name = settings.DOCKER_NETWORK if settings.DOCKER_NETWORK != "host" else None
            else:
                network_name = settings.WORKER_NETWORK

            container_id = await self.create_worker(
                worker_id=worker_id,
                image=image_tag,
                env_vars=container_env,
                volumes=volumes,
                network_name=network_name,
                create_dev_network=network_name is not None,
                workspace_path=str(ws_path),
            )

            await self.docker.exec_in_container(container_id, "chown -R worker:worker /workspace", user="root")

            if repo_id:
                await self.redis.hset(f"worker:meta:{worker_id}", "repo_id", repo_id)

            # Git setup: workspace is pre-scaffolded, just refresh git token
            repo_name = env_vars.get("REPO_NAME")
            github_token = env_vars.get("GITHUB_TOKEN")

            if repo_name and github_token:
                logger.info(
                    "refreshing_git_token",
                    worker_id=worker_id,
                    repo_id=repo_id,
                )
                await git_ops.refresh_git_token(self.docker, container_id, repo_name, github_token, worker_id)

            if branch:
                await git_ops.checkout_branch(self.docker, container_id, branch, worker_id)

            # Inject instructions AFTER git clone (so instruction file doesn't block clone)
            if instructions:
                target_path = agent.get_instruction_path()
                logger.info("injecting_instructions", worker_id=worker_id, path=target_path)

                encoded = base64.b64encode(instructions.encode()).decode()
                cmd = (
                    f'python3 -c "import base64; '
                    f"open('{target_path}', 'w').write("
                    f"base64.b64decode('{encoded}').decode())\""
                )

                exit_code, output = await self.docker.exec_in_container(container_id, cmd)
                if exit_code != 0:
                    container_logs = await self.docker.get_container_logs(container_id)
                    logger.error(
                        "instruction_injection_failed",
                        worker_id=worker_id,
                        error=output,
                        container_logs=container_logs,
                    )

            if task_content:
                task_path = "/workspace/TASK.md"
                logger.info("injecting_task_content", worker_id=worker_id, path=task_path)

                encoded_task = base64.b64encode(task_content.encode()).decode()
                cmd = (
                    f'python3 -c "import base64; '
                    f"open('{task_path}', 'w').write("
                    f"base64.b64decode('{encoded_task}').decode())\""
                )

                exit_code, output = await self.docker.exec_in_container(container_id, cmd)
                if exit_code != 0:
                    container_logs = await self.docker.get_container_logs(container_id)
                    logger.error(
                        "task_injection_failed",
                        worker_id=worker_id,
                        error=output,
                        container_logs=container_logs,
                    )

            return worker_id
        except Exception:
            # Early lock was registered — clean it up on failure
            if project_id:
                await self.redis.srem("workspace:active_projects", project_id)
                await self.redis.delete(
                    f"worker:status:{worker_id}",
                    f"worker:meta:{worker_id}",
                )
            raise
