"""Worker teardown and durable removal-evidence ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Dict

import structlog
from redis.asyncio import Redis

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_evidence import (
    REMOVAL_LOG_TAIL_LINES,
    REMOVAL_LOG_TAIL_MAX_CHARS,
    RemovalFact,
    RemovedWorkerEvidence,
    removed_worker_evidence_key,
    secret_env_values,
)
from shared.diagnostics import redact_diagnostic
from shared.redis import decode_redis_fields

from . import qa_egress
from . import workspace as workspace_mod
from .compose_runner import ComposeRunner
from .config import settings
from .container_config import TRANSCRIPT_MOUNT
from .docker_ops import DockerClientWrapper

logger = structlog.get_logger()

QA_WORKER_TYPE = "qa"

UnregisterBrokerWorker = Callable[[str], Awaitable[None]]
ReleaseWorkspaceLock = Callable[[str, str | None], Awaitable[None]]


class WorkerRemoval:
    """Remove a worker only after preserving its attributable ending."""

    def __init__(
        self,
        redis: Redis,
        docker: DockerClientWrapper,
        *,
        unregister_broker_worker: UnregisterBrokerWorker,
        release_workspace_lock: ReleaseWorkspaceLock,
    ):
        self.redis = redis
        self.docker = docker
        self._unregister_broker_worker = unregister_broker_worker
        self._release_workspace_lock = release_workspace_lock

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
