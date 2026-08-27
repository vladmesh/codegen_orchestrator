"""Fail-closed reader for worker-manager executor diagnostic snapshots."""

from datetime import UTC, datetime, timedelta
import json

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.vocab import AgentType


def unknown_diagnostic(executor: AgentType, reason_code: str) -> ExecutorDiagnostic:
    now = datetime.now(UTC)
    return ExecutorDiagnostic(
        executor=executor,
        enabled=False,
        auth_mode=ExecutorAuthMode.UNKNOWN,
        availability=ExecutorAvailability.UNKNOWN,
        observed_at=now,
        expires_at=now + timedelta(seconds=1),
        active_lease_count=None,
        reason_code=reason_code,
        reason=safe_executor_diagnostic_reason(reason_code),
    )


async def current_executor_snapshot() -> ExecutorDiagnosticSnapshot | None:
    """Read one complete current snapshot, never a cached availability claim."""
    try:
        from .dependencies import get_raw_redis

        raw = await get_raw_redis().get(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        snapshot = ExecutorDiagnosticSnapshot.model_validate(json.loads(raw))
        if snapshot.expires_at <= datetime.now(UTC):
            return None
        return snapshot
    except Exception:
        return None


async def current_executor_diagnostic(
    executor: AgentType,
) -> tuple[ExecutorDiagnostic, ExecutorDiagnosticSnapshot | None]:
    snapshot = await current_executor_snapshot()
    if snapshot is None:
        return unknown_diagnostic(executor, "snapshot_unavailable"), None
    try:
        return snapshot.for_executor(executor, datetime.now(UTC)), snapshot
    except ValueError:
        return unknown_diagnostic(executor, "snapshot_expired"), None
