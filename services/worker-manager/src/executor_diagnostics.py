"""Credential-safe executor availability diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import secrets

from redis.asyncio import Redis
import structlog

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
    INVENTORY_DEPENDENT_PROFILE_CONDITIONS,
    PROFILE_CONDITION_OUTCOMES,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.dto.worker import WORKER_TERMINAL_STATUSES, WorkerStatus
from shared.contracts.queues.worker import WorkerLabel
from shared.contracts.vocab import AgentType
from shared.notifications import deliver_to_admins
from shared.redis import decode_redis_fields, decode_redis_value

from .config import settings
from .docker_ops import DockerClientWrapper
from .profile_alerts import ExecutorProfileAlerts

logger = structlog.get_logger()


class ExecutorDiagnostics:
    """Build and publish one reconciled executor inventory snapshot."""

    def __init__(
        self,
        redis: Redis,
        docker: DockerClientWrapper,
        alerts: ExecutorProfileAlerts | None = None,
    ):
        self.redis = redis
        self.docker = docker
        self.alerts = (
            alerts if alerts is not None else ExecutorProfileAlerts(redis, deliver_to_admins)
        )

    async def publish(self) -> ExecutorDiagnosticSnapshot:
        """Publish one complete short-lived, credential-safe diagnostic snapshot."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=settings.EXECUTOR_DIAGNOSTICS_TTL_SECONDS)
        leases = await self._executor_leases()
        diagnostics = [
            self._executor_diagnostic(AgentType.CLAUDE, now, expires_at, leases),
            self._executor_diagnostic(AgentType.CODEX, now, expires_at, leases),
        ]
        snapshot = ExecutorDiagnosticSnapshot(
            schema_version=EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            version=secrets.token_urlsafe(24),
            observed_at=now,
            expires_at=expires_at,
            diagnostics=diagnostics,
        )
        try:
            await self.redis.set(
                EXECUTOR_DIAGNOSTICS_REDIS_KEY,
                snapshot.model_dump_json(),
                ex=settings.EXECUTOR_DIAGNOSTICS_TTL_SECONDS,
            )
            logger.info("executor_diagnostics_published", version=snapshot.version)
        finally:
            # The same tick owns administrator alerts; it never raises, and a
            # failed publication still alerts on what this tick observed.
            await self.alerts.reconcile(snapshot)
        return snapshot

    async def _executor_leases(self) -> dict[AgentType, int] | None:
        """Return exact executor leases only from a complete two-sided inventory."""
        try:
            metas: dict[str, dict[str, str]] = {}
            async for key in self.redis.scan_iter(match="worker:meta:*"):
                worker_id = str(decode_redis_value(key)).rsplit(":", 1)[-1]
                metas[worker_id] = decode_redis_fields(await self.redis.hgetall(key))
            containers = await self.docker.list_containers(all=True)
            statuses = {
                worker_id: decode_redis_value(
                    await self.redis.hget(f"worker:status:{worker_id}", "status")
                )
                for worker_id in metas
            }
        except Exception as exc:  # noqa: BLE001 — diagnostics report unavailable inventory as unknown
            logger.warning("executor_diagnostics_inventory_unreadable", error=str(exc))
            return None

        containers_by_worker: dict[str, tuple[dict[str, str], str | None]] = {}
        for container in containers:
            labels = (
                getattr(container, "labels", None)
                or getattr(container, "attrs", {}).get("Config", {}).get("Labels", {})
                or {}
            )
            worker_id = labels.get(WorkerLabel.ID.value)
            if worker_id:
                worker_id = str(worker_id)
                if worker_id in containers_by_worker:
                    logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                    return None
                containers_by_worker[worker_id] = (labels, getattr(container, "status", None))

        counts = {AgentType.CLAUDE: 0, AgentType.CODEX: 0}
        for worker_id in set(metas) | set(containers_by_worker):
            meta = metas.get(worker_id)
            container = containers_by_worker.get(worker_id)
            # Docker has no durable owner record to attribute a lease to.  A
            # Redis record is considered first because a refused pre-container
            # create deliberately leaves a terminal status for its caller.
            if meta is None:
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            status_value = statuses.get(worker_id)
            try:
                redis_status = WorkerStatus(status_value)
            except (TypeError, ValueError):
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            if redis_status is WorkerStatus.UNKNOWN:
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            redis_is_terminal = redis_status in WORKER_TERMINAL_STATUSES
            # A terminal refusal can happen before Docker has a container.  It
            # owns no live lease, so it is settled rather than an inventory
            # disagreement.  Nonterminal records still require a counterpart.
            if container is None:
                if redis_is_terminal:
                    continue
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            labels, docker_status = container
            if not self._worker_inventory_labels_match(meta, labels):
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            agent_value = meta.get("agent_type")
            try:
                agent_type = AgentType(agent_value)
            except (TypeError, ValueError):
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            docker_is_terminal = self._docker_worker_is_terminal(docker_status)
            if docker_is_terminal is None or redis_is_terminal != docker_is_terminal:
                logger.warning("executor_diagnostics_inventory_mismatch", worker_id=worker_id)
                return None
            if agent_type in counts and not docker_is_terminal:
                counts[agent_type] += 1
        return counts

    @staticmethod
    def _worker_inventory_labels_match(meta: dict[str, str], labels: dict[str, str]) -> bool:
        """Require the complete credential-safe identity on both inventory sides."""
        expected = {
            WorkerLabel.STORY.value: meta.get("story_id"),
            WorkerLabel.PROJECT.value: meta.get("project_id"),
            WorkerLabel.RUN.value: meta.get("run_id"),
            WorkerLabel.ATTEMPT.value: meta.get("attempt_id"),
            "com.codegen.agent_type": meta.get("agent_type"),
            "com.codegen.auth_mode": meta.get("auth_mode"),
        }
        return all(
            expected_value and labels.get(label) == expected_value
            for label, expected_value in expected.items()
        )

    @staticmethod
    def _docker_worker_is_terminal(status: object) -> bool | None:
        """Map Docker's lifecycle vocabulary without guessing unknown states."""
        if status in {"running", "paused", "restarting", "created"}:
            return False
        if status in {"exited", "dead"}:
            return True
        return None

    def _executor_diagnostic(
        self,
        executor: AgentType,
        now: datetime,
        expires_at: datetime,
        leases: dict[AgentType, int] | None,
    ) -> ExecutorDiagnostic:
        if settings.LIVE_CONTOUR == "stand" and executor is AgentType.CLAUDE:
            failures = self.stand_token_failures()
            failure = next(
                (item for item in failures if item.name == f"{executor.value.title()} token"), None
            )
            if leases is None:
                return ExecutorDiagnostic(
                    executor=executor,
                    enabled=True,
                    auth_mode=ExecutorAuthMode.STAND_TOKEN,
                    availability=ExecutorAvailability.UNKNOWN,
                    observed_at=now,
                    expires_at=expires_at,
                    active_lease_count=None,
                    reason_code="inventory_unreconciled",
                    reason=safe_executor_diagnostic_reason("inventory_unreconciled"),
                )
            if failure is not None:
                return ExecutorDiagnostic(
                    executor=executor,
                    enabled=True,
                    auth_mode=ExecutorAuthMode.STAND_TOKEN,
                    availability=ExecutorAvailability.UNAVAILABLE,
                    observed_at=now,
                    expires_at=expires_at,
                    active_lease_count=leases[executor],
                    reason_code="stand_token_invalid",
                    reason=safe_executor_diagnostic_reason("stand_token_invalid"),
                )
            return ExecutorDiagnostic(
                executor=executor,
                enabled=True,
                auth_mode=ExecutorAuthMode.STAND_TOKEN,
                availability=ExecutorAvailability.AVAILABLE,
                observed_at=now,
                expires_at=expires_at,
                active_lease_count=leases[executor],
                reason_code="stand_token_ready",
                reason=safe_executor_diagnostic_reason("stand_token_ready"),
            )

        profile = (
            settings.HOST_CLAUDE_DIR if executor is AgentType.CLAUDE else settings.HOST_CODEX_HOME
        )
        if not profile:
            return ExecutorDiagnostic(
                executor=executor,
                enabled=False,
                auth_mode=ExecutorAuthMode.HOST_SESSION,
                availability=ExecutorAvailability.UNAVAILABLE,
                observed_at=now,
                expires_at=expires_at,
                active_lease_count=None if leases is None else leases[executor],
                reason_code="disabled",
                reason=safe_executor_diagnostic_reason("disabled"),
            )
        observation = self.inspect_host_profile(executor, profile, now).observation
        reason_code, availability = PROFILE_CONDITION_OUTCOMES[observation.condition]
        # A bad profile is unavailable whatever the inventory says; only an
        # otherwise admissible profile waits on a reconciled lease count.
        if leases is None and observation.condition in INVENTORY_DEPENDENT_PROFILE_CONDITIONS:
            reason_code, availability = "inventory_unreconciled", ExecutorAvailability.UNKNOWN
        return ExecutorDiagnostic(
            executor=executor,
            enabled=True,
            auth_mode=ExecutorAuthMode.HOST_SESSION,
            availability=availability,
            observed_at=now,
            expires_at=expires_at,
            active_lease_count=None if leases is None else leases[executor],
            reason_code=reason_code,
            reason=safe_executor_diagnostic_reason(reason_code),
            profile=observation,
        )

    @staticmethod
    def inspect_host_profile(executor: AgentType, profile: str, now: datetime):
        """Read the manager-visible read-only mount in place, exactly as admission does."""
        if executor is AgentType.CLAUDE:
            from . import claude_auth

            return claude_auth.inspect_claude_host_session(
                settings.HOST_CLAUDE_VALIDATION_PATH or profile, now=now
            )
        from . import codex_auth

        return codex_auth.inspect_codex_host_session(
            settings.HOST_CODEX_VALIDATION_PATH or profile, now=now
        )

    @staticmethod
    def stand_token_failures():
        """Read stand secrets only at the protected manager boundary.

        Public because the manager boundary has two callers: this snapshot and
        `WorkerManager.create_worker_with_capabilities`, which must refuse a
        stand-token worker on the same reading rather than keeping a second one.
        """
        from shared.stand_credentials import CredentialShape, validate_stand_token_credentials

        return validate_stand_token_credentials(
            settings,
            shape=CredentialShape.STAND_HOST,
        )
