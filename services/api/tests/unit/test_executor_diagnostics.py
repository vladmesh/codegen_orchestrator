"""Executor diagnostic snapshots fail closed at the API boundary."""

from datetime import UTC, datetime, timedelta

from shared.contracts.dto.executor_diagnostics import (
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType


def test_snapshot_rejects_extra_fields_and_expired_observations():
    now = datetime.now(UTC)
    diagnostic = ExecutorDiagnostic(
        executor=AgentType.CODEX,
        enabled=True,
        auth_mode="host_session",
        availability=ExecutorAvailability.AVAILABLE,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        active_lease_count=0,
        reason_code="ready",
        reason="Local session and worker inventory are ready.",
    )
    snapshot = ExecutorDiagnosticSnapshot(
        version="opaque-version",
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        diagnostics=[
            diagnostic,
            diagnostic.model_copy(update={"executor": AgentType.CLAUDE}),
        ],
    )

    assert (
        snapshot.for_executor(AgentType.CODEX, now).availability is ExecutorAvailability.AVAILABLE
    )
