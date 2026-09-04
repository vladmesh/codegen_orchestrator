import base64
import hashlib
from pathlib import Path
import json
import os
import secrets
from datetime import datetime
from typing import Optional, Dict, List

import structlog
import httpx
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.dto.executor_diagnostics import ExecutorDiagnosticSnapshot
from shared.constants import Timeouts
from shared.contracts.queues.worker import DeleteWorkerCommand, WorkerLabel, WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.qa_probe_cli import QA_PROBE_PATH, QA_PROBE_SCRIPT
from shared.redis import decode_redis_fields, decode_redis_value
from shared.queues import WORKER_COMMANDS

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import WORKER_SOURCE_HASH_LABEL, ImageBuilder, get_base_image
from .container_config import TRANSCRIPT_MOUNT, WorkerContainerConfig
from .executor_diagnostics import ExecutorDiagnostics
from . import workspace as workspace_mod
from . import garbage_collector as gc
from . import git_ops
from . import qa_egress
from .worker_removal import QA_WORKER_TYPE, WorkerRemoval

logger = structlog.get_logger()

# What a `dev_proj_<worker_id>` network says it is, in `com.codegen.type`. A
# network is created and destroyed with its worker but is a separate Docker
# object, so it carries the worker's ownership itself: a run that has to remove
# its own resources after a crash finds this network by
# `com.codegen.run.id=<run>` alone, without knowing the worker id the name is
# built from — which is exactly what is unrecoverable once the container and its
# Redis metadata are gone.
DEV_NETWORK_TYPE_LABEL = "worker-dev-network"


class WorkerManager:
    """
    Manages worker container lifecycle.
    """

    def __init__(self, redis: Redis, docker_client: Optional[DockerClientWrapper] = None):
        self.redis = redis
        self.docker = docker_client or DockerClientWrapper()
        self._executor_diagnostics = ExecutorDiagnostics(self.redis, self.docker)
        self._worker_removal = WorkerRemoval(
            self.redis,
            self.docker,
            unregister_broker_worker=self._unregister_broker_worker,
            release_workspace_lock=self._release_workspace_lock,
        )

    async def publish_executor_diagnostics(self) -> ExecutorDiagnosticSnapshot:
        """Publish one complete short-lived, credential-safe diagnostic snapshot."""
        return await self._executor_diagnostics.publish()

    async def delete_worker(self, worker_id: str, reason: str | None = None) -> None:
        """Stop and remove a worker, its dev network, workspace, and Redis keys."""
        await self._worker_removal.delete_worker(worker_id, reason)

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

    async def _prepare_remote_daemon_mounts(
        self,
        *,
        image: str,
        worker_id: str,
        workspace_path: str,
        transcript_path: str,
    ) -> None:
        """Prepare bind mounts in the namespace that will launch the worker.

        With the host socket, worker-manager and Docker resolve a bind source to the same
        filesystem, so ``prepare_worker_paths`` is authoritative. A TCP/SSH daemon may resolve
        the same spelling in another mount namespace (the integration DinD daemon does), making
        the manager-side chown irrelevant. In that topology a short, networkless helper on the
        target daemon owns the two mounts before the non-root worker starts.
        """
        docker_host = os.getenv("DOCKER_HOST", "").strip()
        if not docker_host or docker_host.startswith("unix://"):
            return

        helper_name = f"worker-mount-prep-{worker_id}"
        await self.docker.remove_container(helper_name, force=True)
        try:
            await self.docker.run_container(
                image,
                name=helper_name,
                entrypoint="/bin/chown",
                command=["-R", "1000:1000", "/workspace", TRANSCRIPT_MOUNT],
                user="root",
                network_mode="none",
                volumes={
                    workspace_path: {"bind": "/workspace", "mode": "rw"},
                    transcript_path: {"bind": TRANSCRIPT_MOUNT, "mode": "rw"},
                },
                remove=True,
                read_only=True,
                cap_drop=["ALL"],
                cap_add=["CHOWN", "DAC_OVERRIDE"],
                security_opt=["no-new-privileges:true"],
                pids_limit=32,
                mem_limit="64m",
            )
        except Exception as exc:
            raise RuntimeError(f"remote Docker daemon could not prepare worker mounts for {worker_id}: {exc}") from exc

    async def _stamp_ownership(
        self,
        worker_id: str,
        ownership: WorkerOwnership,
        *,
        agent_type: AgentType | None = None,
        auth_mode: str | None = None,
        worker_type: str | None = None,
    ) -> None:
        """Write who this worker belongs to, before the container exists.

        The single writer of the fact. Every caller passes the same required
        `ownership` value threaded down from the create request, so the record
        cannot be half-written or disagree with the container's labels.
        """
        metadata = ownership.as_redis_meta()
        if agent_type is not None and auth_mode is not None:
            metadata.update({"agent_type": agent_type.value, "auth_mode": auth_mode})
        if worker_type is not None:
            metadata["worker_type"] = worker_type
        await self.redis.hset(f"worker:meta:{worker_id}", mapping=metadata)

    async def _acquire_workspace_lock(
        self,
        worker_id: str,
        ownership: WorkerOwnership,
        *,
        agent_type: AgentType = AgentType.CLAUDE,
        auth_mode: str = "host_session",
        worker_type: str = "developer",
    ) -> str:
        """Take the project's workspace lock for this worker, or refuse it.

        Acquisition decides whether a developer worker exists at all; ownership
        describes a worker that does. Both happen here, in this order, and
        nowhere else for a developer worker.

        The ownership stamp goes in first because `project_id` is the evidence
        the workspace garbage collector reads: it clears a project from
        `workspace:active_projects` when no worker's metadata claims it, and it
        is an independent task in this process. If the set membership became
        visible before the metadata, that sweep could run in the window, judge
        the project stale, and let a second creator onto the same checkout.

        The lock stores its owner, not merely a set membership.  That fence is
        what prevents an old delete from releasing a newer worker's checkout.
        """
        await self._stamp_ownership(
            worker_id,
            ownership,
            agent_type=agent_type,
            auth_mode=auth_mode,
            worker_type=worker_type,
        )
        lock_key = f"workspace:lock:{ownership.project_id}"
        acquired = await self.redis.set(lock_key, worker_id, nx=True)
        if not acquired:
            await self.redis.delete(f"worker:meta:{worker_id}")
            raise RuntimeError(f"Project {ownership.project_id} workspace lock was taken by a concurrent worker")
        await self.redis.sadd("workspace:active_projects", ownership.project_id)
        return ownership.project_id

    async def _release_workspace_lock(self, worker_id: str, held_project_id: str | None) -> None:
        """Give the workspace back, if this worker is the one that took it.

        `held_project_id` is the project this worker acquired — either the
        return of `_acquire_workspace_lock` or, once the worker is only a Redis
        record, the `project_id` of a developer worker, which exists exactly
        because that worker acquired. A QA executor and a worker refused before
        acquisition are both excluded before they reach here: neither took the
        workspace, and releasing on their behalf frees a live worker's checkout
        under it.
        """
        if not held_project_id:
            return
        lock_key = f"workspace:lock:{held_project_id}"
        owner = await self.redis.get(lock_key)
        owner = owner.decode() if isinstance(owner, bytes) else owner
        if owner is None:
            # Pre-fence workers have only the old set membership. This path is
            # reached only after Docker removal was confirmed.
            await self.redis.srem("workspace:active_projects", held_project_id)
            logger.info("legacy_workspace_lock_released", project_id=held_project_id, worker_id=worker_id)
            return
        if owner != worker_id:
            logger.warning(
                "workspace_lock_not_released_not_owner",
                project_id=held_project_id,
                worker_id=worker_id,
                owner=owner,
            )
            return
        await self.redis.delete(lock_key)
        await self.redis.srem("workspace:active_projects", held_project_id)
        logger.info("workspace_lock_released", project_id=held_project_id, worker_id=worker_id)

    async def _reject_worker(self, worker_id: str, exc: Exception) -> None:
        """Mark a worker that was refused before it could take anything.

        A refusal has to be terminal in Redis. The create command is ACKed early
        and the caller then polls `worker:status`, so a rejected worker with no
        status is one the caller waits out the full readiness timeout for and
        then publishes a delete for — turning a refusal into a deletion that
        would otherwise reach for someone else's lock.
        """
        logger.warning("worker_rejected", worker_id=worker_id, error=str(exc))
        await self.redis.set(f"worker:error:{worker_id}", str(exc))
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})

    async def _fail_acquired_worker(self, worker_id: str, exc: Exception) -> None:
        """Publish terminal state and durable teardown intent for an acquired worker."""
        await self.redis.set(f"worker:error:{worker_id}", str(exc))
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})
        await self.redis.xadd(
            WORKER_COMMANDS,
            {
                "data": DeleteWorkerCommand(
                    request_id=f"cleanup-{worker_id}", worker_id=worker_id, reason="creation_failed"
                ).model_dump_json()
            },
        )

    async def create_worker(
        self,
        worker_id: str,
        image: str,
        *,
        ownership: WorkerOwnership,
        env_vars: Dict[str, str] = None,
        volumes: Dict[str, Dict[str, str]] = None,
        network_name: Optional[str] = None,
        create_dev_network: bool = True,
        workspace_path: Optional[str] = None,
        container_config: Optional[WorkerContainerConfig] = None,
        allow_host_network: bool = False,
        publish_ready: bool = True,
    ) -> str:
        """
        Create and start a new worker container.

        Args:
            ownership: the project, run and attempt this worker belongs to. Applied to the
                container's labels and written to `worker:meta:<worker_id>` before
                the container exists, so a worker that dies immediately — and whose
                Redis metadata is deleted with it — is still attributable from
                `docker ps -a --filter label=...` alone.
            network_name: Primary Docker network to attach to. If None, uses WORKER_NETWORK.
            create_dev_network: If True, also create a dev_proj_<worker_id> network and
                                connect the container to it as a second network.
            workspace_path: Host path to the worker workspace (stored in Redis metadata).
            publish_ready: mark the worker RUNNING after the container starts. QA
                workers defer this until their injected turn materials are ready.
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
        labels[WorkerLabel.ID.value] = worker_id
        labels[WorkerLabel.TYPE.value] = "worker"
        labels["com.codegen.agent_type"] = container_config.agent_type.value
        labels["com.codegen.auth_mode"] = container_config.auth_mode
        labels.update(ownership.as_labels())

        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        dev_network = f"dev_proj_{worker_id}"

        logger.info(
            "creating_worker",
            worker_id=worker_id,
            image=image,
            container_name=container_name,
            network=network_name,
            dev_network=dev_network if create_dev_network else None,
            project_id=ownership.project_id,
            run_id=ownership.run_id,
            attempt_id=ownership.attempt_id,
        )

        try:
            # Ownership is written before the container exists, on both sides.
            # A container that dies in its first second has already carried its
            # labels since creation, and the metadata was already there.
            await self._stamp_ownership(
                worker_id,
                ownership,
                agent_type=container_config.agent_type,
                auth_mode=container_config.auth_mode,
            )

            await self.docker.remove_container(container_name, force=True)

            if create_dev_network:
                await self.docker.create_network(
                    dev_network,
                    labels={**labels, WorkerLabel.TYPE.value: DEV_NETWORK_TYPE_LABEL},
                )

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

            if publish_ready:
                await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})

            return container.id

        except Exception as e:
            logger.error("worker_creation_failed", worker_id=worker_id, error=str(e))
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})
            await self.redis.set(f"worker:error:{worker_id}", str(e))
            raise

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

    async def get_worker_status(self, worker_id: str) -> str:
        """Read the status Redis holds; UNKNOWN when it holds none.

        Redis is the only source consulted. A worker whose status key has
        expired or was never written is reported UNKNOWN rather than
        inspected in Docker, so this call stays free of a daemon round trip.
        """
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
        """Check if another developer worker is active for this project.

        Returns worker_id if locked, None if free.

        The owner-fenced key is authoritative.  The active-projects set is a
        legacy discovery aid only and must not turn a missing set member into
        permission to reuse a workspace while an owner key remains.
        """
        lock_owner = await self.redis.get(f"workspace:lock:{project_id}")
        if lock_owner:
            return decode_redis_value(lock_owner)
        if not await self.redis.sismember("workspace:active_projects", project_id):
            return None
        async for key in self.redis.scan_iter(match="worker:meta:*"):
            meta = decode_redis_fields(await self.redis.hgetall(key))
            if meta.get("worker_type") == QA_WORKER_TYPE:
                continue
            if meta.get("project_id") == project_id:
                worker_id = key.split(":")[-1]
                return worker_id
        return None

    async def create_worker_with_capabilities(
        self,
        worker_id: str,
        capabilities: List[str],
        base_image: str,
        ownership: WorkerOwnership,
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
        repo_id: str | None = None,
        branch: str | None = None,
    ) -> str:
        """
        Create worker with specified capabilities and agent config.
        Injects instructions (-> instruction file) and task_content (-> TASK.md) if provided.

        `ownership` is the project, the run that asked for the work and the
        attempt inside it that the requester made this worker for. It is
        required — a worker nobody owns cannot be attributed once it is dead —
        and it is written below, once this worker is one that will exist, and
        always before a container of it can.
        """
        logger.info(
            "create_worker_with_capabilities",
            worker_id=worker_id,
            project_id=ownership.project_id,
            run_id=ownership.run_id,
            attempt_id=ownership.attempt_id,
            worker_type=worker_type,
        )
        agent_type = AgentType(agent_type)
        is_qa_worker = worker_type == QA_WORKER_TYPE
        project_id = ownership.project_id
        env_vars = env_vars or {}
        workspace_path = None
        factory_api_key = None
        if auth_mode == "stand_token":
            if agent_type is not AgentType.CLAUDE:
                raise RuntimeError("stand_token authentication is supported only for Claude workers")
            if agent_type is AgentType.CLAUDE and (api_key or "ANTHROPIC_API_KEY" in env_vars):
                raise RuntimeError("ANTHROPIC_API_KEY conflicts with Claude stand_token authentication")
            supplied = {"CLAUDE_CODE_OAUTH_TOKEN"}.intersection(env_vars)
            if supplied:
                raise RuntimeError(
                    "stand token credentials must be local to worker-manager, not env_vars: "
                    + ", ".join(sorted(supplied))
                )
            failure = next(
                (
                    item
                    for item in self._executor_diagnostics.stand_token_failures()
                    if item.name == f"{agent_type.value.title()} token"
                ),
                None,
            )
            if failure is not None:
                raise RuntimeError(f"stand_token authentication is unavailable: {failure.detail}")

        # These checks can refuse a request before it owns metadata, a workspace
        # fence, or a cleanup command. A terminal status still tells the early-
        # ACKed caller to stop polling without manufacturing teardown state.
        held_project_id: str | None = None
        try:
            network_name, allow_host_network = self._resolve_worker_network(for_qa=is_qa_worker)

            if agent_type == AgentType.CODEX and auth_mode == "host_session":
                from .codex_auth import validate_codex_host_session

                validation_path = settings.HOST_CODEX_VALIDATION_PATH or host_codex_home
                validate_codex_host_session(validation_path)
            if agent_type == AgentType.CLAUDE and auth_mode == "host_session" and host_claude_dir:
                from .claude_auth import validate_claude_host_session

                validate_claude_host_session(settings.HOST_CLAUDE_VALIDATION_PATH or host_claude_dir)

            if is_qa_worker:
                if not instructions or not task_content:
                    raise RuntimeError(
                        "a QA executor requires instructions and task_content before it can become ready"
                    )
            else:
                if not repo_id:
                    raise RuntimeError(
                        "repo_id is required — all developer workers must use pre-scaffolded "
                        "workspaces. Ensure scaffolder has run before spawning workers."
                    )

            if agent_type == AgentType.FACTORY:
                factory_api_key = env_vars.get("FACTORY_API_KEY") or api_key or os.getenv("FACTORY_API_KEY")
                if not factory_api_key:
                    raise RuntimeError("FACTORY_API_KEY is not set")

            # The workspace lock is a developer-worker concern: it guards the one
            # persistent checkout a project has. A QA executor owns the same project
            # but touches no workspace of it, so it neither takes the lock nor is
            # blocked by one — its ownership is a record, not a claim.
            if not is_qa_worker:
                existing_worker = await self._check_project_lock(project_id)
                if existing_worker:
                    raise RuntimeError(f"Project {project_id} already has active worker {existing_worker}")

                failure_key = f"workspace:{project_id}:failure_count"
                failure_count = int(await self.redis.get(failure_key) or 0)

                if failure_count >= 3:
                    raise RuntimeError(
                        f"Max retries (3) exceeded for project {project_id}. Reset with: DEL {failure_key}"
                    )

                workspace_path, scaffolded_exists = workspace_mod.get_scaffolded_workspace(
                    settings.SCAFFOLDED_WORKSPACE_PATH, repo_id
                )
                if not scaffolded_exists:
                    raise RuntimeError(
                        f"Scaffolded workspace not found for repo_id={repo_id} at {workspace_path}. "
                        "Scaffolder must run first."
                    )

                # Take the project and, with it, stamp ownership — early, so the
                # spawner gets worker_id before the image build, and long before
                # anything can produce a container.
                held_project_id = await self._acquire_workspace_lock(
                    worker_id,
                    ownership,
                    agent_type=agent_type,
                    auth_mode=auth_mode,
                    worker_type=worker_type,
                )
            else:
                # A QA executor takes no lock, so nothing gates its ownership:
                # it is stamped as soon as this is known to be a worker that
                # will exist, and still before any container of it does.
                await self._stamp_ownership(
                    worker_id,
                    ownership,
                    agent_type=agent_type,
                    auth_mode=auth_mode,
                    worker_type=worker_type,
                )
        except Exception as exc:
            # Acquisition is the only thing in the block that takes anything,
            # and it either succeeded or withdrew what it wrote — so the release
            # path is asked with what was actually acquired, not assumed.
            await self._release_workspace_lock(worker_id, held_project_id)
            await self._reject_worker(worker_id, exc)
            raise

        if not is_qa_worker:
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.BUILDING})

        prefix = prefix or settings.WORKER_IMAGE_PREFIX
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
                stand_claude_code_oauth_token=(
                    settings.STAND_CLAUDE_CODE_OAUTH_TOKEN if auth_mode == "stand_token" else None
                ),
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
                ws_path = workspace_path
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
                # The wrapper enforces this shared per-turn limit. Do not let a
                # worker-manager-only environment variable create a second,
                # earlier ceiling than the metadata and waiter advertise.
                subprocess_timeout_seconds=Timeouts.AGENT_TURN,
            )
            container_env.update(env_vars)
            for forbidden in ("WORKER_REDIS_URL", "WORKER_API_URL", "WORKER_MANAGER_URL", "SECRETS_ENCRYPTION_KEY"):
                container_env.pop(forbidden, None)
            if factory_api_key is not None:
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
                    # The run's proxy belongs to the run that opened it, and is
                    # labelled with the same ownership as the executor it serves.
                    labels={**json.loads(settings.WORKER_DOCKER_LABELS), **ownership.as_labels()},
                )
                container_env.update(egress.env_vars)

            workspace_mod.prepare_worker_paths(
                workspace_path=config.workspace_host_path,
                transcript_path=config.transcript_host_path,
            )
            await self._prepare_remote_daemon_mounts(
                image=image_tag,
                worker_id=worker_id,
                workspace_path=config.workspace_host_path,
                transcript_path=config.transcript_host_path,
            )
            volumes = config.to_volume_mounts()

            container_id = await self.create_worker(
                worker_id=worker_id,
                image=image_tag,
                ownership=ownership,
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
                # A QA container starts before its injected files exist. Keep
                # it STARTING until AGENTS/CLAUDE, TASK and /workspace/qa are
                # all usable, so the central runner cannot publish its turn to
                # a partial workspace.
                publish_ready=not is_qa_worker,
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
                    raise RuntimeError(f"could not inject {target_path} for {worker_id}: {output}")

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
                    raise RuntimeError(f"could not inject {task_path} for {worker_id}: {output}")

            if is_qa_worker:
                await self._inject_qa_probe(container_id, worker_id)
                await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})
                logger.info("qa_executor_ready", worker_id=worker_id)

            return worker_id
        except Exception as exc:
            # This block starts after ownership has been stamped and, for a
            # developer, after its workspace fence has been acquired.  A
            # container may already be running when an instruction, checkout,
            # or QA setup step fails.  Do not erase the only ownership record
            # or free its checkout here: `delete_worker` is the teardown owner
            # and releases both only after Docker confirms removal.
            if is_qa_worker:
                logger.warning("qa_worker_creation_failed", worker_id=worker_id, error=str(exc))
            else:
                logger.warning("developer_worker_creation_failed", worker_id=worker_id, error=str(exc))
            await self._fail_acquired_worker(worker_id, exc)
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
