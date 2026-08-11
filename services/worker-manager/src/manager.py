import base64
import hashlib
from pathlib import Path
import json
import os
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, List

import structlog
import httpx
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.vocab import AgentType
from shared.qa_probe_cli import QA_PROBE_PATH, QA_PROBE_SCRIPT
from shared.redis import decode_redis_fields

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import WORKER_SOURCE_HASH_LABEL, ImageBuilder, get_base_image
from .container_config import WorkerContainerConfig
from . import workspace as workspace_mod
from .compose_runner import ComposeRunner
from . import garbage_collector as gc
from . import git_ops
from . import qa_egress

if TYPE_CHECKING:
    from shared.contracts.queues.worker import ScaffoldConfig

logger = structlog.get_logger()

# The central exploratory-QA executor. It differs from a developer worker in
# what it is given, not in how it is started: no repository, no git credentials,
# an empty workspace that is deleted with the container, and one injected
# command that is its only route to the deployment under test.
QA_WORKER_TYPE = "qa"


class WorkerManager:
    """
    Manages worker container lifecycle.
    Replaces legacy ContainerService and LifecycleManager.
    """

    def __init__(self, redis: Redis, docker_client: Optional[DockerClientWrapper] = None):
        self.redis = redis
        self.docker = docker_client or DockerClientWrapper()

    async def _register_broker_worker(self, worker_id: str, token: str, worker_type: str) -> None:
        """Register a worker-scoped credential before its container is started.

        The credential carries the worker's type because the type is what the
        broker authorizes on. It is sent here, from the service that decided
        what kind of worker this is, and never accepted from the worker.
        """
        from shared.contracts.queues.worker import WorkerChannels

        payload = {
            "worker_id": worker_id,
            "token": token,
            "worker_type": worker_type,
            "input_stream": WorkerChannels.INPUT_PATTERN.value.format(worker_id=worker_id),
            "output_stream": WorkerChannels.OUTPUT_PATTERN.value.format(worker_id=worker_id),
            "session_ttl_seconds": settings.WORKER_BROKER_SESSION_TTL_SECONDS,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{settings.WORKER_BROKER_URL.rstrip('/')}/internal/workers",
                json=payload,
                headers={"X-Broker-Internal-Token": settings.WORKER_BROKER_INTERNAL_TOKEN},
            )
            response.raise_for_status()
        await self.redis.hset(
            f"worker:broker:{worker_id}", mapping={"token_digest": hashlib.sha256(token.encode()).hexdigest()}
        )

    async def _unregister_broker_worker(self, worker_id: str) -> None:
        """Revoke a deleted worker's broker metadata without exposing its credential."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.delete(
                    f"{settings.WORKER_BROKER_URL.rstrip('/')}/internal/workers/{worker_id}",
                    headers={"X-Broker-Internal-Token": settings.WORKER_BROKER_INTERNAL_TOKEN},
                )
                response.raise_for_status()
        except Exception:
            logger.warning("broker_worker_unregistration_failed", worker_id=worker_id)

    async def ensure_image(self, image: str) -> None:
        """Ensure image exists and update access time."""
        exists = await self.docker.image_exists(image)
        if not exists:
            logger.info("pulling_image", image=image)
            await self.docker.pull_image(image)

        await self.redis.set(f"worker:image:last_used:{image}", datetime.now().isoformat())

    def _resolve_worker_network(self, *, for_qa: bool = False) -> tuple[str, bool]:
        """Return the worker network and whether a test-only host mode is permitted.

        A QA executor does not get the shared worker network at all. It gets its
        own internal one, whose whole purpose is that nothing routes off it, and
        it never gets host networking — under `host` the container would share
        the management host's stack and every guarantee here would be a comment.
        """
        if for_qa:
            qa_network = settings.QA_EGRESS_NETWORK.strip()
            if not qa_network:
                raise RuntimeError("QA_EGRESS_NETWORK must name a dedicated internal Docker network")
            if qa_network == "host":
                raise RuntimeError("a QA executor cannot use host networking")
            return qa_network, False

        configured_network = settings.DOCKER_NETWORK.strip()
        network_name = configured_network or settings.WORKER_NETWORK.strip()
        if not network_name:
            raise RuntimeError("WORKER_NETWORK must name a dedicated Docker network")

        if network_name == "host":
            if settings.ENVIRONMENT != "test":
                raise RuntimeError("DOCKER_NETWORK=host is only supported when ENVIRONMENT=test")
            return network_name, True

        return network_name, False

    async def create_worker(
        self,
        worker_id: str,
        image: str,
        env_vars: Dict[str, str] = None,
        volumes: Dict[str, Dict[str, str]] = None,
        network_name: Optional[str] = None,
        create_dev_network: bool = True,
        workspace_path: Optional[str] = None,
        container_config: Optional[WorkerContainerConfig] = None,
        allow_host_network: bool = False,
    ) -> str:
        """
        Create and start a new worker container.

        Args:
            network_name: Primary Docker network to attach to. If None, uses WORKER_NETWORK.
            create_dev_network: If True, also create a dev_proj_<worker_id> network and
                                connect the container to it as a second network.
            workspace_path: Host path to the worker workspace (stored in Redis metadata).
        """
        env_vars = env_vars or {}
        network_name = network_name or settings.WORKER_NETWORK
        container_config = container_config or WorkerContainerConfig(
            worker_id=worker_id,
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            capabilities=[],
        )

        if allow_host_network and settings.ENVIRONMENT != "test":
            raise RuntimeError("Host networking is only supported when ENVIRONMENT=test")

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
            network=network_name,
            dev_network=dev_network if create_dev_network else None,
        )

        try:
            await self.docker.remove_container(container_name, force=True)

            if create_dev_network:
                await self.docker.create_network(dev_network)

            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STARTING})

            run_kwargs = container_config.to_docker_run_kwargs(
                network_name=network_name,
                allow_host_network=allow_host_network,
            )
            run_kwargs.update(
                {
                    "image": image,
                    "name": container_name,
                    "environment": env_vars,
                    "labels": labels,
                    "volumes": volumes,
                }
            )

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
        is_qa_worker = bool(meta) and meta.get("worker_type") == QA_WORKER_TYPE

        try:
            # A QA executor's workspace is scratch created for one run: there is
            # no compose project in it to bring down, and it does not survive
            # the container. A developer workspace is the opposite on both
            # counts and is preserved here.
            if is_qa_worker:
                workspace_mod.remove_workspace(
                    settings.SCAFFOLDED_WORKSPACE_PATH,
                    f"{workspace_mod.QA_WORKSPACE_PREFIX}{worker_id}",
                )
                logger.info("qa_workspace_removed", worker_id=worker_id)
                # The run's egress proxy is as ephemeral as the run: it holds
                # the second network leg the executor is not allowed to have, so
                # it must not outlive the container it was opened for.
                await qa_egress.tear_down(self.docker, worker_id)
            elif stored_workspace:
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
            await self._unregister_broker_worker(worker_id)
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
                f"worker:broker:{worker_id}",
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

        # The generated Dockerfile builds FROM the agent-specific base, so the cache key must
        # include that base's source hash — otherwise a rebuilt base is masked by a stale tag.
        agent_base = get_base_image(agent_type)
        source_hash = await self.docker.get_image_label(agent_base, WORKER_SOURCE_HASH_LABEL)
        if not source_hash or source_hash == "unknown":
            raise RuntimeError(
                f"Base image {agent_base} carries no {WORKER_SOURCE_HASH_LABEL} label "
                f"(got {source_hash!r}). Run 'make rebuild-worker-images'."
            )

        image_tag = builder.get_image_tag(
            capabilities=capabilities,
            prefix=prefix,
            agent_type=agent_type,
            source_hash=source_hash,
        )

        exists = await self.docker.image_exists(image_tag)

        if not exists:
            logger.info(
                "image_cache_miss",
                image_tag=image_tag,
                capabilities=capabilities,
                agent_type=agent_type,
                source_hash=source_hash,
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
            worker_type=worker_type,
        )
        is_qa_worker = worker_type == QA_WORKER_TYPE
        # Written before anything is created, for two reasons that both need it
        # early. It is what `delete_worker` reads to know a QA workspace is
        # scratch it must remove — a creation that fails halfway would otherwise
        # leave a directory nothing owns. And it is the server's record of what
        # this worker is, which the Compose route authorizes on: the record has
        # to exist before the credential does, because a request whose worker
        # type is unrecorded is refused.
        await self.redis.hset(f"worker:meta:{worker_id}", "worker_type", worker_type)

        network_name, allow_host_network = self._resolve_worker_network(for_qa=is_qa_worker)

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
                transcript_host_path=settings.WORKER_TRANSCRIPT_STORAGE_PATH,
                transcript_max_bytes=settings.WORKER_TRANSCRIPT_MAX_BYTES,
            )
            self._prune_transcripts()

            # A developer worker must be handed the repository the scaffolder
            # prepared. A QA executor must not be handed a repository at all:
            # it tests a running deployment as a black box, and a checkout in
            # its workspace would be an invitation to read implementation for
            # evidence and something to accidentally leave behind.
            if is_qa_worker:
                ws_path = workspace_mod.create_ephemeral_workspace(settings.SCAFFOLDED_WORKSPACE_PATH, worker_id)
                logger.info("using_ephemeral_qa_workspace", worker_id=worker_id, path=str(ws_path))
            else:
                if not repo_id:
                    raise RuntimeError(
                        "repo_id is required — all developer workers must use pre-scaffolded "
                        "workspaces. Ensure scaffolder has run before spawning workers."
                    )
                ws_path, scaffolded_exists = workspace_mod.get_scaffolded_workspace(
                    settings.SCAFFOLDED_WORKSPACE_PATH, repo_id
                )
                if not scaffolded_exists:
                    raise RuntimeError(
                        f"Scaffolded workspace not found for repo_id={repo_id} at {ws_path}. Scaffolder must run first."
                    )
                logger.info(
                    "using_scaffolded_workspace",
                    worker_id=worker_id,
                    repo_id=repo_id,
                    path=str(ws_path),
                )
            config.workspace_host_path = str(ws_path)

            broker_token = secrets.token_urlsafe(32)
            await self._register_broker_worker(worker_id, broker_token, worker_type)
            container_env = config.to_env_vars(
                broker_url=settings.WORKER_BROKER_URL,
                broker_token=broker_token,
                subprocess_timeout_seconds=settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS,
            )
            container_env.update(env_vars)
            for forbidden in ("WORKER_REDIS_URL", "WORKER_API_URL", "WORKER_MANAGER_URL", "SECRETS_ENCRYPTION_KEY"):
                container_env.pop(forbidden, None)
            if agent_type == AgentType.FACTORY and "FACTORY_API_KEY" not in container_env:
                factory_api_key = os.getenv("FACTORY_API_KEY")
                if not factory_api_key:
                    raise RuntimeError("FACTORY_API_KEY is not set")
                container_env["FACTORY_API_KEY"] = factory_api_key

            github_token = env_vars.get("GITHUB_TOKEN")
            if github_token:
                container_env["GH_TOKEN"] = github_token

            # The egress policy is put in place before the container that lives
            # under it exists, and it raises rather than degrading: a QA run
            # never starts with an unrestricted container. What the executor is
            # told about the proxy is a convenience for its CLI — the boundary
            # is the internal network it is about to be attached to.
            if is_qa_worker:
                egress = await qa_egress.establish(
                    self.docker,
                    worker_id=worker_id,
                    agent_type=agent_type,
                    image=image_tag,
                    network=network_name,
                    internet_network=settings.WORKER_NETWORK,
                    configured_backends=self._qa_backend_setting(agent_type),
                    direct=qa_egress.direct_hosts(container_env, settings.WORKER_BROKER_URL),
                    labels=json.loads(settings.WORKER_DOCKER_LABELS),
                )
                container_env.update(egress.env_vars)

            workspace_mod.prepare_worker_paths(
                workspace_path=config.workspace_host_path,
                transcript_path=config.transcript_host_path,
            )
            volumes = config.to_volume_mounts()

            container_id = await self.create_worker(
                worker_id=worker_id,
                image=image_tag,
                env_vars=container_env,
                volumes=volumes,
                network_name=network_name,
                # A QA executor runs no project of its own, and a second network
                # is exactly what it must not have: it is attached to the QA
                # egress network alone, where the only things it can address are
                # the run's capability endpoint, the broker, and its own proxy.
                create_dev_network=network_name != "host" and not is_qa_worker,
                workspace_path=str(ws_path),
                container_config=config,
                allow_host_network=allow_host_network,
            )
            if is_qa_worker:
                # Proof, not intent: whatever was asked for, this is what Docker
                # actually attached. A container that ended up on a second
                # network — a leftover default, a hand-edited compose, a future
                # branch here — can reach the deployment directly, so it is
                # refused before it is given any work.
                qa_egress.verify_isolation(await self.docker.inspect_container(container_id), network_name)

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

            if is_qa_worker:
                await self._inject_qa_probe(container_id, worker_id)

            return worker_id
        except Exception as exc:
            await self._unregister_broker_worker(worker_id)
            if is_qa_worker:
                # The run's door out is removed with the run it was opened for,
                # including a run that never got going.
                await qa_egress.tear_down(self.docker, worker_id)
                # The container may already be up and marked RUNNING by
                # `create_worker`, while the step that failed is the one that
                # installs the executor's only route to the deployment. A QA
                # client polling status would then send a run into a container
                # that has to improvise. Say it failed.
                await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})
                await self.redis.set(f"worker:error:{worker_id}", str(exc))
            # Early lock was registered — clean it up on failure
            if project_id:
                await self.redis.srem("workspace:active_projects", project_id)
                await self.redis.delete(
                    f"worker:status:{worker_id}",
                    f"worker:meta:{worker_id}",
                )
            raise

    @staticmethod
    def _qa_backend_setting(agent_type: AgentType) -> str:
        """The operator override for this agent's model backend, if there is one."""
        if agent_type == AgentType.CODEX:
            return settings.QA_CODEX_BACKEND_HOSTS
        return settings.QA_CLAUDE_BACKEND_HOSTS

    async def _inject_qa_probe(self, container_id: str, worker_id: str) -> None:
        """Put the QA executor's one command into its workspace.

        This is the whole of what the container can reach the deployment with.
        It carries no address and no credential of its own — both arrive in the
        environment, from the QA runtime that issued them for this run — so a
        copy of this file is worth nothing anywhere else.

        A failure here is fatal to the run and must not be a logged warning: an
        executor without this command would go looking for another way to reach
        the application, which is exactly what must not happen.
        """
        encoded = base64.b64encode(QA_PROBE_SCRIPT.encode()).decode()
        cmd = (
            f'python3 -c "import base64, os; '
            f"p = '{QA_PROBE_PATH}'; "
            f"open(p, 'w').write(base64.b64decode('{encoded}').decode()); "
            f'os.chmod(p, 0o755)"'
        )
        exit_code, output = await self.docker.exec_in_container(container_id, cmd)
        if exit_code != 0:
            raise RuntimeError(f"could not install the QA capability command in {worker_id}: {output}")
        logger.info("qa_probe_installed", worker_id=worker_id, path=QA_PROBE_PATH)

    def _prune_transcripts(self) -> None:
        """Delete expired disk artifacts without affecting worker creation."""
        try:
            root = Path(settings.WORKER_TRANSCRIPT_STORAGE_PATH)
            root.mkdir(parents=True, exist_ok=True)
            cutoff = datetime.now().timestamp() - settings.WORKER_TRANSCRIPT_RETENTION_DAYS * 86400
            for artifact in root.rglob("*.log"):
                if artifact.stat().st_mtime < cutoff:
                    artifact.unlink()
        except OSError as exc:
            logger.warning("transcript_retention_cleanup_failed", error=str(exc))
