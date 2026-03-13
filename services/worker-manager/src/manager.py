import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Dict, List

import httpx
import structlog
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import ImageBuilder
from .container_config import WorkerContainerConfig
from . import workspace as workspace_mod
from .compose_runner import ComposeRunner

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
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STARTING})

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

        # Retrieve metadata stored at creation time
        meta = await self.redis.hgetall(f"worker:meta:{worker_id}")
        dev_network = meta.get("dev_network") if meta else None
        stored_workspace = meta.get("workspace_path") if meta else None

        try:
            # Tear down sidecar containers launched via compose before removing worker
            if stored_workspace:
                try:
                    runner = ComposeRunner(settings.WORKSPACE_BASE_PATH)
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

            # Remove worker container
            await self.docker.remove_container(container_name, force=True)

            # Remove dev network
            if dev_network:
                await self.docker.remove_network(dev_network)

            # Remove workspace directory from host (preserve project workspaces)
            project_id = meta.get("project_id") if meta else None
            if project_id:
                logger.info("workspace_preserved", project_id=project_id, worker_id=worker_id)
                await self.redis.srem("workspace:active_projects", project_id)

                # Track consecutive failures per project for retry logic
                if reason:
                    failure_key = f"workspace:{project_id}:failure_count"
                    if reason in ("failed", "timeout"):
                        await self.redis.incr(failure_key)
                        await self.redis.expire(failure_key, 48 * 3600)
                    elif reason == "completed":
                        await self.redis.delete(failure_key)
            else:
                workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, worker_id)

            # Clean up all Redis keys for this worker
            keys_to_delete = [
                f"worker:status:{worker_id}",
                f"worker:meta:{worker_id}",
                f"worker:error:{worker_id}",
                f"worker:last_activity:{worker_id}",
                f"worker:{worker_id}:input",
                f"worker:{worker_id}:output",
            ]
            await self.redis.delete(*keys_to_delete)

        except Exception as e:
            logger.error("worker_deletion_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STOPPED})

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

    async def garbage_collect_orphaned_resources(self) -> None:
        """Find and remove orphaned containers, networks, and workspaces.

        After a crash/OOM, resources may be left behind without corresponding
        Redis state. This method collects the set of known worker IDs from Redis
        and removes any Docker containers, dev networks, or workspace directories
        that don't belong to a known worker.

        Order: containers → networks → workspaces (networks can't be removed
        while containers are connected).
        """
        # Collect known worker IDs from Redis
        known_ids: set[str] = set()
        async for key in self.redis.scan_iter(match="worker:status:*"):
            known_ids.add(key.split(":")[-1])

        logger.info("orphan_gc_start", known_workers=len(known_ids))

        # --- Orphaned containers ---
        try:
            containers = await self.docker.list_containers(filters={"label": "com.codegen.type=worker"}, all=True)
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
                    await self.delete_worker(worker_id)
                except Exception as e:
                    logger.error("orphan_gc_delete_worker_failed", worker_id=worker_id, error=str(e))
            elif worker_id:
                live_container_ids.add(worker_id)

        # --- Stale Redis entries (Redis says alive, but no container) ---
        for worker_id in known_ids:
            if worker_id not in live_container_ids:
                status = await self.redis.hget(f"worker:status:{worker_id}", "status")
                if status and status not in self._TERMINAL_STATUSES:
                    logger.warning(
                        "orphan_gc_stale_redis",
                        worker_id=worker_id,
                        redis_status=status,
                    )
                    try:
                        await self.delete_worker(worker_id)
                    except Exception as e:
                        logger.error(
                            "orphan_gc_stale_cleanup_failed",
                            worker_id=worker_id,
                            error=str(e),
                        )

        # --- Orphaned networks ---
        try:
            networks = await self.docker.list_networks()
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
                        await self.docker.remove_network(name)
                    except Exception as e:
                        logger.error("orphan_gc_remove_network_failed", network=name, error=str(e))

        # --- Orphaned workspaces ---
        active_projects = await self.redis.smembers("workspace:active_projects")

        try:
            entries = os.listdir(settings.WORKSPACE_BASE_PATH)
        except FileNotFoundError:
            entries = []
        except Exception as e:
            logger.error("orphan_gc_list_workspaces_failed", error=str(e))
            entries = []

        for entry in entries:
            if entry not in known_ids and entry not in active_projects:
                logger.info("orphan_gc_removing_workspace", worker_id=entry)
                try:
                    workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, entry)
                except Exception as e:
                    logger.error("orphan_gc_remove_workspace_failed", worker_id=entry, error=str(e))

        logger.info("orphan_gc_complete")

    async def garbage_collect_workspaces(self, max_age_hours: int = 35) -> None:
        """Remove project workspaces older than max_age_hours with no active workers.

        Scans both WORKSPACE_BASE_PATH (project workspaces) and
        SCAFFOLDED_WORKSPACE_PATH (scaffolder output). Also cleans
        stale workspace:active_projects entries.
        """
        # Clean stale active_projects entries — remove projects with no live worker
        active_projects = await self.redis.smembers("workspace:active_projects")
        for project_id in active_projects:
            has_worker = False
            async for key in self.redis.scan_iter(match="worker:meta:*"):
                meta = await self.redis.hgetall(key)
                if meta.get("project_id") == project_id:
                    has_worker = True
                    break
            if not has_worker:
                await self.redis.srem("workspace:active_projects", project_id)
                logger.info("workspace_gc_cleared_stale_project", project_id=project_id)

        # Refresh after cleanup
        active_projects = await self.redis.smembers("workspace:active_projects")

        now = time.time()
        paths_to_scan = [
            settings.WORKSPACE_BASE_PATH,
            settings.SCAFFOLDED_WORKSPACE_PATH,
        ]
        for base_path in paths_to_scan:
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
                    await self._notify_workspace_deleted(entry)
                    logger.info(
                        "workspace_gc_removed",
                        project_id=entry,
                        base_path=base_path,
                        age_hours=round(age_hours, 1),
                    )

    async def _notify_workspace_deleted(self, repo_id: str) -> None:
        """Notify API that a workspace was GC'd so workspace_ready is cleared."""
        api_url = settings.WORKER_API_URL or "http://api:8000"
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

    # Statuses that indicate the worker is no longer alive and can be cleaned up
    _TERMINAL_STATUSES = frozenset({WorkerStatus.DEAD, WorkerStatus.FAILED, WorkerStatus.STOPPED})

    async def _check_project_lock(self, project_id: str) -> str | None:
        """Check if another worker is active for this project.

        Returns worker_id if locked, None if free.
        Auto-cleans stale Redis keys for workers in terminal states (DEAD/FAILED/STOPPED).
        """
        if not await self.redis.sismember("workspace:active_projects", project_id):
            return None
        # Find which worker holds the lock
        async for key in self.redis.scan_iter(match="worker:meta:*"):
            meta = await self.redis.hgetall(key)
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
        return None  # stale set entry, safe to proceed

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
        project_id: str | None = None,
        repo_id: str | None = None,
        scaffold_config: "ScaffoldConfig | None" = None,
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

        # Check project mutex — only one worker per project at a time
        if project_id:
            existing_worker = await self._check_project_lock(project_id)
            if existing_worker:
                raise RuntimeError(f"Project {project_id} already has active worker {existing_worker}")

            # Check failure count — reject after MAX_CONSECUTIVE_FAILURES, force clean after 2
            failure_key = f"workspace:{project_id}:failure_count"
            failure_count = int(await self.redis.get(failure_key) or 0)

            if failure_count >= 3:
                raise RuntimeError(f"Max retries (3) exceeded for project {project_id}. Reset with: DEL {failure_key}")

            if failure_count >= 2:
                workspace_mod.remove_workspace(settings.WORKSPACE_BASE_PATH, project_id)
                logger.warning(
                    "workspace_force_cleaned",
                    project_id=project_id,
                    failure_count=failure_count,
                )

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

        # Resolve workspace on the host
        if repo_id:
            # Pre-scaffolded workspace from scaffolder service
            ws_path, scaffolded_exists = workspace_mod.get_scaffolded_workspace(
                settings.SCAFFOLDED_WORKSPACE_PATH, repo_id
            )
            if not scaffolded_exists:
                raise RuntimeError(
                    f"Scaffolded workspace not found for repo_id={repo_id} at {ws_path}. Scaffolder must run first."
                )
            config.workspace_host_path = str(ws_path)
            workspace_existed = True
            logger.info(
                "using_scaffolded_workspace",
                worker_id=worker_id,
                repo_id=repo_id,
                path=str(ws_path),
            )
        elif project_id:
            ws_path, workspace_existed = workspace_mod.get_or_create_project_workspace(
                settings.WORKSPACE_BASE_PATH, project_id
            )
            config.workspace_host_path = str(ws_path)
        else:
            ws_path = workspace_mod.create_workspace(settings.WORKSPACE_BASE_PATH, worker_id)
            workspace_existed = False
            config.workspace_host_path = workspace_mod.get_workspace_host_path(settings.WORKSPACE_BASE_PATH, worker_id)

        # Generate container params
        # WORKER_REDIS_URL/WORKER_API_URL override for DinD (workers can't resolve compose DNS).
        # Default: bridge-network URLs (services reachable via Docker DNS in bridge mode).
        worker_redis_url = settings.WORKER_REDIS_URL or "redis://redis:6379"
        worker_api_url = settings.WORKER_API_URL or "http://api:8000"
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

        # DOCKER_NETWORK overrides WORKER_NETWORK (used in CI/integration tests).
        # Empty DOCKER_NETWORK (default) = use WORKER_NETWORK (codegen_worker).
        # Workers attach to codegen_worker (isolated from orchestrator infra),
        # not codegen_internal (which has the orchestrator's db/redis).
        if settings.DOCKER_NETWORK:
            network_name = settings.DOCKER_NETWORK if settings.DOCKER_NETWORK != "host" else None
        else:
            network_name = settings.WORKER_NETWORK

        # Create container (dev network only when not in host mode)
        container_id = await self.create_worker(
            worker_id=worker_id,
            image=image_tag,
            env_vars=container_env,
            volumes=volumes,
            network_name=network_name,
            create_dev_network=network_name is not None,
            workspace_path=str(ws_path),
        )

        # Fix workspace ownership inside the container.
        # In DinD environments, bind-mount UID mapping can differ from the host,
        # so the host-side chown may not take effect. This exec ensures /workspace
        # is writable by the worker user regardless of the Docker environment.
        await self.docker.exec_in_container(container_id, "chown -R worker:worker /workspace", user="root")

        # Persist project_id in Redis meta and active projects set
        if project_id:
            await self.redis.hset(f"worker:meta:{worker_id}", "project_id", project_id)
            await self.redis.sadd("workspace:active_projects", project_id)

        # Scaffold/git phase
        repo_name = env_vars.get("REPO_NAME")
        github_token = env_vars.get("GITHUB_TOKEN")

        if repo_id and repo_name and github_token:
            # Pre-scaffolded workspace: just refresh git token (scaffolder already
            # cloned, ran copier + make setup, committed and pushed)
            logger.info(
                "scaffold_phase_skipped_repo_id",
                worker_id=worker_id,
                repo_id=repo_id,
                reason="workspace pre-scaffolded by scaffolder service",
            )
            await self._refresh_git_token(container_id, repo_name, github_token, worker_id)
        elif scaffold_config and repo_name and github_token:
            logger.info(
                "scaffold_phase_entering",
                worker_id=worker_id,
                template=scaffold_config.template_repo,
                project_name=scaffold_config.project_name,
                modules=scaffold_config.modules,
            )
            scaffold_ok = await self._run_scaffold_phase(
                container_id, scaffold_config, repo_name, github_token, worker_id
            )
            if not scaffold_ok:
                # Cleanup on failure
                logger.error("scaffold_phase_failed_cleanup", worker_id=worker_id)
                await self.delete_worker(worker_id)
                raise RuntimeError(f"Scaffold phase failed for worker {worker_id}")

            # Verify scaffold produced expected markers
            verify_cmd = (
                "bash -c '"
                "test -f /workspace/.copier-answers.yml "
                "&& test -d /workspace/.github/workflows "
                "&& echo SCAFFOLD_OK || echo SCAFFOLD_MISSING'"
            )
            exit_code, marker_output = await self.docker.exec_in_container(container_id, verify_cmd, timeout=10)
            if isinstance(marker_output, bytes):
                marker_output = marker_output.decode()
            marker_output = marker_output.strip()
            if marker_output != "SCAFFOLD_OK":
                logger.error(
                    "scaffold_markers_missing_after_scaffold",
                    worker_id=worker_id,
                    marker_check=marker_output,
                    hint="copier ran but did not produce .copier-answers.yml or .github/workflows/",
                )
                await self.delete_worker(worker_id)
                raise RuntimeError(
                    f"Scaffold markers missing after scaffold phase for worker {worker_id}. "
                    "copier may have failed silently."
                )

            logger.info(
                "scaffold_phase_verified",
                worker_id=worker_id,
                markers="copier-answers + github-workflows present",
            )
        elif repo_name and github_token:
            # Normal path: clone existing repo (feature/fix actions)
            logger.info(
                "scaffold_phase_skipped",
                worker_id=worker_id,
                reason="no scaffold_config — existing repo will be cloned",
                has_scaffold_config=scaffold_config is not None,
            )
            if workspace_existed:
                await self._refresh_git_token(container_id, repo_name, github_token, worker_id)
                logger.info("workspace_reused", project_id=project_id, worker_id=worker_id)
            else:
                await self._setup_git_repo(container_id, repo_name, github_token, worker_id)

        # Inject instructions AFTER git clone (so instruction file doesn't block clone)
        if instructions:
            target_path = agent.get_instruction_path()
            logger.info("injecting_instructions", worker_id=worker_id, path=target_path)

            # Use base64 encoding to avoid shell quoting issues
            # Worker base has python3 installed
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

    async def _refresh_git_token(self, container_id: str, repo: str, token: str, worker_id: str) -> bool:
        """Update git remote URL with fresh token in existing workspace."""
        script = f"cd /workspace && git remote set-url origin 'https://x-access-token:{token}@github.com/{repo}'"
        encoded = base64.b64encode(script.encode()).decode()
        cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"
        exit_code, output = await self.docker.exec_in_container(container_id, cmd, timeout=30)
        if exit_code != 0:
            logger.error("git_token_refresh_failed", worker_id=worker_id, error=output)
            return False
        logger.info("git_token_refreshed", worker_id=worker_id, repo=repo)
        return True

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

    async def _run_scaffold_phase(
        self,
        container_id: str,
        scaffold_config: "ScaffoldConfig",
        repo: str,
        token: str,
        worker_id: str,
    ) -> bool:
        """Run copier + make setup + git push inside the worker container.

        This replaces the old scaffolder service. Copier is installed via
        `uv tool install copier` (cached in uv-cache volume after first run).

        Args:
            container_id: Docker container ID
            scaffold_config: ScaffoldConfig with template, project name, modules
            repo: Repository in "owner/repo" format
            token: GitHub access token
            worker_id: Worker ID for logging

        Returns:
            True if scaffold succeeded, False otherwise
        """
        logger.info(
            "scaffold_phase_start",
            worker_id=worker_id,
            template=scaffold_config.template_repo,
            project_name=scaffold_config.project_name,
            modules=scaffold_config.modules,
        )

        # Base64-encode task_description to safely pass it through bash
        # without shell metacharacter issues (quotes, backticks, $(), etc.)
        task_desc_b64 = base64.b64encode(scaffold_config.task_description.encode()).decode()

        # Build scaffold script (runs as worker user inside container)
        scaffold_script = f"""set -e

# Clone the repo (created via GitHub API with auto_init)
cd /workspace
git clone "https://x-access-token:{token}@github.com/{repo}" .tmp-clone
# Move .git into workspace root (copier will overwrite files, not .git)
mv .tmp-clone/.git /workspace/.git
rm -rf .tmp-clone

git config user.email "ai@codegen.local"
git config user.name "Codegen Bot"
git config core.hooksPath /dev/null

# Install copier via uv (cached after first run)
uv tool install copier

# Write task_description to YAML data file (avoids shell escaping issues)
echo -n '{task_desc_b64}' | base64 -d > /tmp/_copier_desc.txt
printf 'task_description: |\\n' > /tmp/copier-data.yml
sed 's/^/  /' /tmp/_copier_desc.txt >> /tmp/copier-data.yml
rm /tmp/_copier_desc.txt

# Run copier to scaffold project
copier copy {scaffold_config.template_repo} /workspace \
    --data "project_name={scaffold_config.project_name}" \
    --data "modules={scaffold_config.modules}" \
    --data-file /tmp/copier-data.yml \
    --trust --defaults --overwrite --vcs-ref=HEAD

# Setup project (install deps, generate code)
cd /workspace
make setup

# Stage, commit, push
git add .
git commit --no-verify -m "feat: scaffold {scaffold_config.project_name} with modules: {scaffold_config.modules}" || true
git push origin main

# Re-enable hooks for the agent
git config core.hooksPath .githooks
"""

        encoded = base64.b64encode(scaffold_script.encode()).decode()
        cmd = f"bash -c 'echo {encoded} | base64 -d | bash'"

        exit_code, output = await self.docker.exec_in_container(
            container_id,
            cmd,
            timeout=600,  # 10 min for copier + make setup
        )

        if exit_code != 0:
            logger.error(
                "scaffold_phase_failed",
                worker_id=worker_id,
                exit_code=exit_code,
                error=output,
            )
            return False

        logger.info("scaffold_phase_complete", worker_id=worker_id, repo=repo)
        return True
