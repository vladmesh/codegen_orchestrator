import asyncio
import base64
import hashlib
from pathlib import Path
import json
import os
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional, Dict, List

import structlog
import httpx
from redis.asyncio import Redis

from shared.contracts.dto.worker import WorkerStatus
from shared.constants import Timeouts
from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.contracts.worker_evidence import (
    REMOVAL_LOG_TAIL_LINES,
    REMOVAL_LOG_TAIL_MAX_CHARS,
    RemovalFact,
    RemovedWorkerEvidence,
    removed_worker_evidence_key,
    secret_env_values,
)
from shared.diagnostics import redact_diagnostic
from shared.qa_probe_cli import QA_PROBE_PATH, QA_PROBE_SCRIPT
from shared.redis import decode_redis_fields, decode_redis_value

from .config import settings
from .docker_ops import DockerClientWrapper
from .image_builder import WORKER_SOURCE_HASH_LABEL, ImageBuilder, get_base_image
from .container_config import TRANSCRIPT_MOUNT, WorkerContainerConfig
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

    async def _stamp_ownership(self, worker_id: str, ownership: WorkerOwnership) -> None:
        """Write who this worker belongs to, before the container exists.

        The single writer of the fact. Every caller passes the same required
        `ownership` value threaded down from the create request, so the record
        cannot be half-written or disagree with the container's labels.
        """
        await self.redis.hset(f"worker:meta:{worker_id}", mapping=ownership.as_redis_meta())

    async def _acquire_workspace_lock(self, worker_id: str, ownership: WorkerOwnership) -> str:
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
        await self._stamp_ownership(worker_id, ownership)
        lock_key = f"workspace:lock:{ownership.project_id}"
        acquired = await self.redis.set(lock_key, worker_id, nx=True)
        if not acquired:
            await self.redis.hdel(f"worker:meta:{worker_id}", *ownership.as_redis_meta())
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
        await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})
        await self.redis.set(f"worker:error:{worker_id}", str(exc))

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
            await self._stamp_ownership(worker_id, ownership)

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

    @staticmethod
    def _ownership_from_meta(meta: Dict[str, str] | None) -> WorkerOwnership | None:
        """The worker's own ownership, or None if its record does not carry one.

        Every worker is stamped with all three facts before its container can
        exist, so the None case is a worker whose metadata is already gone —
        a second delete, or a container the garbage collector adopted. There is
        nothing to key a run-scoped record by then, and inventing a run to file
        it under would be worse than saying so in the log.
        """
        if not meta:
            return None
        if not all(meta.get(field) for field in ("project_id", "run_id", "attempt_id")):
            return None
        return WorkerOwnership(
            project_id=meta["project_id"],
            run_id=meta["run_id"],
            attempt_id=meta["attempt_id"],
        )

    async def _capture_removal_evidence(
        self,
        worker_id: str,
        container_name: str,
        meta: Dict[str, str] | None,
        ownership: WorkerOwnership,
        reason: str | None,
    ) -> bool:
        """Write down how this worker ended, before the container that knows is removed.

        This is the last instant the fact exists. Docker forgets a removed
        container entirely — labels included — so a worker deleted before anyone
        looked at it is unattributable unless the remover itself wrote the
        ending down. That is what this does, into a run-scoped record that the
        `finally` block's deletion of `worker:meta` does not touch.

        **Capture never owns the deletion.** It is bounded by
        `WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS`, it raises nothing at its
        caller, and a fact it could not read becomes a stated reason rather than
        an absence: a worker that cannot be captured is still removed, and
        cleanup is never wedged by observability.

        Returns whether a durable record now exists, which is what the caller
        needs to know before it deletes the worker's last durable name.
        """
        timeout = settings.WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS
        read = self._read_removal_evidence(worker_id, container_name, meta, ownership, reason)
        try:
            evidence = await asyncio.wait_for(read, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a failed read is evidence, not a failed delete
            # Including a bound that could not even be applied: a read this
            # never got to start is still a read this closes behind itself.
            read.close()
            evidence = self._unreadable_removal_evidence(
                worker_id,
                container_name,
                meta,
                ownership,
                reason,
                f"the container could not be read before it was removed: {type(exc).__name__}: {exc}",
            )
        store = self._store_removal_evidence(evidence)
        try:
            await asyncio.wait_for(store, timeout=timeout)
            logger.info(
                "worker_removal_evidence_captured",
                worker_id=worker_id,
                run_id=ownership.run_id,
                exit_code=evidence.exit_code.value,
                exit_code_missed=evidence.exit_code.missed_reason,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — see above
            store.close()
            # The record is the durable half and it is what failed. Saying so in
            # the log is not enough — a log line is not a source the run's
            # artifact reads — so the caller keeps `worker:meta:<id>` instead,
            # and the worker stays nameable to its run.
            logger.warning(
                "worker_removal_evidence_not_stored",
                worker_id=worker_id,
                run_id=ownership.run_id,
                error=str(exc),
            )
            return False

    def _unreadable_removal_evidence(
        self,
        worker_id: str,
        container_name: str,
        meta: Dict[str, str] | None,
        ownership: WorkerOwnership,
        reason: str | None,
        missed: str,
    ) -> RemovedWorkerEvidence:
        """A record for a worker whose container could not be read at all.

        Still a record. "This worker existed, was removed, and here is why
        nothing about its ending could be read" is a finding; an absent record
        reads as "nothing ran", which is the failure this evidence exists to end.
        """
        return RemovedWorkerEvidence(
            worker_id=worker_id,
            container=container_name,
            ownership=ownership,
            removed_at=datetime.now(tz=UTC).isoformat(),
            delete_reason=reason,
            worker_type=self._worker_type_fact(meta),
            agent_type=RemovalFact.missed(missed),
            image=RemovalFact.missed(missed),
            state=RemovalFact.missed(missed),
            exit_code=RemovalFact.missed(missed),
            log_tail=RemovalFact.missed(missed),
            transcript_dir=RemovalFact.missed(missed),
        )

    @staticmethod
    def _worker_type_fact(meta: Dict[str, str] | None) -> RemovalFact:
        worker_type = meta.get("worker_type") if meta else None
        if worker_type:
            return RemovalFact.read(worker_type)
        return RemovalFact.missed("this worker's Redis metadata no longer names its type")

    async def _read_removal_evidence(
        self,
        worker_id: str,
        container_name: str,
        meta: Dict[str, str] | None,
        ownership: WorkerOwnership,
        reason: str | None,
    ) -> RemovedWorkerEvidence:
        """Read the ending off the container while there still is one."""
        inspected = await self.docker.inspect_container(container_name)
        environment: Dict[str, str] = {}
        for entry in inspected["Config"]["Env"] or []:
            name, _, value = entry.partition("=")
            environment[name] = value
        state = inspected["State"]

        if state["Running"]:
            # `delete_worker` force-removes, so a worker deleted while it was
            # still working never produces an exit code. That is a fact about
            # the deletion, not an agent that exited cleanly with 0.
            exit_code = RemovalFact.missed(
                "the container was still running when it was removed, so it never had an exit code"
            )
        elif state["Status"] == "created":
            exit_code = RemovalFact.missed("the container was created but never started, so it has no exit code")
        else:
            exit_code = RemovalFact.read(int(state["ExitCode"]))

        try:
            raw = await self.docker.read_container_logs(container_name, tail=REMOVAL_LOG_TAIL_LINES)
            log_tail = RemovalFact.read(self._bounded_tail(raw, environment))
        except Exception as exc:  # noqa: BLE001 — the exit code is still worth keeping
            log_tail = RemovalFact.missed(
                f"the container's log could not be read before removal: {type(exc).__name__}: {exc}"
            )

        agent_type = environment.get("WORKER_AGENT_TYPE")
        transcript_dir = None
        for mount in inspected["Mounts"] or []:
            if mount["Destination"] == TRANSCRIPT_MOUNT:
                transcript_dir = f"{mount['Source']}/{worker_id}"
                break

        return RemovedWorkerEvidence(
            worker_id=worker_id,
            container=container_name,
            ownership=ownership,
            removed_at=datetime.now(tz=UTC).isoformat(),
            delete_reason=reason,
            worker_type=self._worker_type_fact(meta),
            agent_type=(
                RemovalFact.read(agent_type)
                if agent_type
                else RemovalFact.missed("the container declares no WORKER_AGENT_TYPE")
            ),
            image=RemovalFact.read({"tag": inspected["Config"]["Image"], "id": inspected["Image"]}),
            state=RemovalFact.read(
                {
                    "status": state["Status"],
                    "running": bool(state["Running"]),
                    "oom_killed": bool(state["OOMKilled"]),
                    "started_at": state["StartedAt"],
                    "finished_at": state["FinishedAt"],
                    "error": state["Error"],
                }
            ),
            exit_code=exit_code,
            log_tail=log_tail,
            transcript_dir=(
                RemovalFact.read(transcript_dir)
                if transcript_dir
                else RemovalFact.missed(
                    f"the container declares no {TRANSCRIPT_MOUNT} bind mount, so no "
                    "retained transcript can be pointed at"
                )
            ),
        )

    @staticmethod
    def _bounded_tail(raw: str, environment: Dict[str, str]) -> str:
        """Bound and redact one log tail before it is persisted.

        The tail is the container's own structlog output, never agent stdout —
        that stays in the transcript worker-wrapper retains, which this record
        points at by path. What is kept is the *end* of the tail: the last lines
        before a worker died are the ones that say why.
        """
        redacted = redact_diagnostic(raw, secrets=secret_env_values(environment))
        if len(redacted) > REMOVAL_LOG_TAIL_MAX_CHARS:
            return redacted[-REMOVAL_LOG_TAIL_MAX_CHARS:]
        return redacted

    async def _store_removal_evidence(self, evidence: RemovedWorkerEvidence) -> None:
        """Persist one record under its run, outside anything `delete_worker` deletes."""
        key = removed_worker_evidence_key(evidence.ownership.run_id)
        await self.redis.hset(key, evidence.worker_id, evidence.model_dump_json())
        await self.redis.expire(key, settings.WORKER_REMOVAL_EVIDENCE_TTL_SECONDS)

    async def delete_worker(self, worker_id: str, reason: str | None = None) -> None:
        """Stop and remove a worker, its dev network, workspace, and Redis keys."""
        container_name = f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}"
        logger.info("deleting_worker", worker_id=worker_id)

        meta = decode_redis_fields(await self.redis.hgetall(f"worker:meta:{worker_id}"))
        dev_network = meta.get("dev_network") if meta else None
        stored_workspace = meta.get("workspace_path") if meta else None
        is_qa_worker = bool(meta) and meta.get("worker_type") == QA_WORKER_TYPE
        # What this worker acquired. For a developer worker that is its
        # `project_id`, which exists only because the worker acquired: one
        # refused before acquisition carries none. A QA executor is the one
        # worker that owns a project whose workspace it never took, so it is
        # excluded here and releases nothing.
        held_project_id = (meta.get("project_id") if meta else None) if not is_qa_worker else None
        # Who this worker belonged to, read before anything is torn down: it is
        # what the removal record below is filed under, and it is only knowable
        # from the metadata this method is about to delete.
        ownership = self._ownership_from_meta(meta)
        # `worker:meta:<id>` is this worker's last durable name: once it is gone
        # and the container with it, nothing left can say the worker existed.
        # It is therefore deleted only after the removal record exists — a
        # worker whose metadata was never keyed to a run has no record to wait
        # for and nothing a leaked key could be attributed to.
        keep_meta = False
        removed = False
        evidence: RemovedWorkerEvidence | None = None
        if ownership is None:
            logger.warning(
                "worker_removal_evidence_unattributable",
                worker_id=worker_id,
                error="this worker's metadata names no project, run and attempt to file its ending under",
            )

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

            # Read while Docker can still describe the container, but do not
            # publish removal evidence until `remove_container` succeeds.
            if ownership is not None:
                try:
                    evidence_task = asyncio.create_task(
                        self._read_removal_evidence(worker_id, container_name, meta, ownership, reason)
                    )
                    evidence = await asyncio.wait_for(
                        evidence_task,
                        timeout=settings.WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    evidence = self._unreadable_removal_evidence(
                        worker_id,
                        container_name,
                        meta,
                        ownership,
                        reason,
                        "the container could not be read before it was removed: "
                        f"{type(exc).__name__}: {exc or 'inspection timed out'}",
                    )

            await self.docker.remove_container(container_name, force=True)
            removed = True

            if evidence is not None:
                try:
                    await self._store_removal_evidence(
                        evidence.model_copy(update={"removed_at": datetime.now(tz=UTC).isoformat()})
                    )
                except Exception as exc:  # noqa: BLE001
                    keep_meta = True
                    logger.warning("worker_removal_evidence_not_stored", worker_id=worker_id, error=str(exc))

            if dev_network:
                await self.docker.remove_network(dev_network)

        except Exception as e:
            logger.error("worker_deletion_failed", worker_id=worker_id, error=str(e))
        if not removed:
            # No evidence and no lock release: a failed Docker call is not a
            # teardown confirmation. The supervisor re-drives the durable stop
            # intent with capped backoff until this operation succeeds.
            return
        await self._unregister_broker_worker(worker_id)
        # Only the worker that took the workspace lock releases it, and the
        # holder fact is the only thing that says so.
        if held_project_id:
            logger.info("workspace_preserved", project_id=held_project_id, worker_id=worker_id)
            await self._release_workspace_lock(worker_id, held_project_id)

            if reason:
                failure_key = f"workspace:{held_project_id}:failure_count"
                if reason in ("failed", "timeout"):
                    await self.redis.incr(failure_key)
                    await self.redis.expire(failure_key, 48 * 3600)
                elif reason == "completed":
                    await self.redis.delete(failure_key)

        keys_to_delete = [
            f"worker:status:{worker_id}",
            f"worker:error:{worker_id}",
            f"worker:broker:{worker_id}",
            f"worker:active-turn:{worker_id}",
            f"worker:{worker_id}:input",
            f"worker:{worker_id}:output",
        ]
        if keep_meta:
            logger.warning(
                "worker_meta_retained_for_attribution",
                worker_id=worker_id,
                run_id=ownership.run_id,
                error="no removal record could be stored, so the worker keeps its last durable name",
            )
        else:
            keys_to_delete.append(f"worker:meta:{worker_id}")
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
        scaffold_config: "ScaffoldConfig | None" = None,
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
        is_qa_worker = worker_type == QA_WORKER_TYPE
        project_id = ownership.project_id
        # Written before anything is created, for two reasons that both need it
        # early. It is what `delete_worker` reads to know a QA workspace is
        # scratch it must remove — a creation that fails halfway would otherwise
        # leave a directory nothing owns. And it is the server's record of what
        # this worker is, which the Compose route authorizes on: the record has
        # to exist before the credential does, because a request whose worker
        # type is unrecorded is refused.
        await self.redis.hset(f"worker:meta:{worker_id}", "worker_type", worker_type)

        # Everything up to the lock is a refusal, not a failure of a worker that
        # started: it took nothing, so nothing is released here. What it must
        # leave behind is a terminal status, so the caller that was ACKed early
        # stops polling now instead of timing out and deleting a worker that
        # never held anything.
        held_project_id: str | None = None
        try:
            network_name, allow_host_network = self._resolve_worker_network(for_qa=is_qa_worker)

            if agent_type == AgentType.CODEX and auth_mode == "host_session":
                from .codex_auth import validate_codex_host_session

                validation_path = settings.HOST_CODEX_VALIDATION_PATH or host_codex_home
                validate_codex_host_session(validation_path)

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

                # Take the project and, with it, stamp ownership — early, so the
                # spawner gets worker_id before the image build, and long before
                # anything can produce a container.
                held_project_id = await self._acquire_workspace_lock(worker_id, ownership)
            else:
                # A QA executor takes no lock, so nothing gates its ownership:
                # it is stamped as soon as this is known to be a worker that
                # will exist, and still before any container of it does.
                await self._stamp_ownership(worker_id, ownership)
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
        env_vars = env_vars or {}

        try:
            if is_qa_worker and (not instructions or not task_content):
                raise RuntimeError("a QA executor requires instructions and task_content before it can become ready")
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
                # The wrapper enforces this shared per-turn limit. Do not let a
                # worker-manager-only environment variable create a second,
                # earlier ceiling than the metadata and waiter advertise.
                subprocess_timeout_seconds=Timeouts.AGENT_TURN,
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
            # Early lock was registered — clean it up on failure, reading the
            # holder fact rather than assuming it, and before the metadata that
            # holds it is deleted. A QA executor took no lock and must not
            # release a developer worker's.
            if not is_qa_worker:
                await self._release_workspace_lock(worker_id, held_project_id)
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
