"""Executor diagnostic snapshots fail closed at the API boundary."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from pydantic import ValidationError
import pytest

from shared.contracts.dto.executor_diagnostics import (
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType
from src.executor_diagnostics import current_executor_diagnostic, current_executor_snapshot


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
        reason="Local authentication and worker inventory are ready.",
    )
    snapshot = ExecutorDiagnosticSnapshot(
        schema_version="v1",
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


def test_snapshot_rejects_a_fresh_outer_window_with_an_expired_executor_entry():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ExecutorDiagnosticSnapshot(
            schema_version="v1",
            version="mixed-freshness",
            observed_at=now,
            expires_at=now + timedelta(seconds=60),
            diagnostics=[
                ExecutorDiagnostic(
                    executor=AgentType.CODEX,
                    enabled=True,
                    auth_mode="host_session",
                    availability=ExecutorAvailability.AVAILABLE,
                    observed_at=now,
                    expires_at=now + timedelta(seconds=60),
                    active_lease_count=0,
                    reason_code="ready",
                    reason="Local authentication and worker inventory are ready.",
                ),
                ExecutorDiagnostic(
                    executor=AgentType.CLAUDE,
                    enabled=True,
                    auth_mode="host_session",
                    availability=ExecutorAvailability.AVAILABLE,
                    observed_at=now - timedelta(seconds=120),
                    expires_at=now - timedelta(seconds=60),
                    active_lease_count=0,
                    reason_code="ready",
                    reason="Local authentication and worker inventory are ready.",
                ),
            ],
        )


def test_snapshot_requires_protocol_version_and_fixed_safe_reason_text():
    now = datetime.now(UTC)
    item = {
        "executor": "codex",
        "enabled": True,
        "auth_mode": "host_session",
        "availability": "available",
        "observed_at": now,
        "expires_at": now + timedelta(seconds=60),
        "active_lease_count": 0,
        "reason_code": "ready",
        "reason": "/home/operator/.codex refresh_token=not-safe",
    }
    with pytest.raises(ValidationError):
        ExecutorDiagnostic.model_validate(item)

    item["reason"] = "Local authentication and worker inventory are ready."
    with pytest.raises(ValidationError):
        ExecutorDiagnosticSnapshot.model_validate(
            {
                "version": "opaque-version",
                "observed_at": now,
                "expires_at": now + timedelta(seconds=60),
                "diagnostics": [item, {**item, "executor": "claude"}],
            }
        )


@pytest.mark.parametrize(
    ("enabled", "auth_mode", "availability", "leases", "reason_code"),
    [
        (True, "host_session", ExecutorAvailability.AVAILABLE, 0, "ready"),
        (True, "api_key", ExecutorAvailability.DEGRADED, 1, "local_warning"),
        (True, "host_session", ExecutorAvailability.UNAVAILABLE, 2, "local_auth_invalid"),
        (True, "api_key", ExecutorAvailability.UNAVAILABLE, 2, "api_key_missing"),
        (False, "host_session", ExecutorAvailability.UNAVAILABLE, 3, "disabled"),
        (False, "api_key", ExecutorAvailability.UNAVAILABLE, None, "disabled"),
        (True, "host_session", ExecutorAvailability.UNKNOWN, None, "inventory_unreconciled"),
        (False, "unknown", ExecutorAvailability.UNKNOWN, None, "snapshot_unavailable"),
        (False, "unknown", ExecutorAvailability.UNKNOWN, None, "snapshot_expired"),
    ],
)
def test_diagnostic_contract_accepts_only_closed_semantic_states(
    enabled, auth_mode, availability, leases, reason_code
):
    from shared.contracts.dto.executor_diagnostics import safe_executor_diagnostic_reason

    now = datetime.now(UTC)
    diagnostic = ExecutorDiagnostic(
        executor=AgentType.CODEX,
        enabled=enabled,
        auth_mode=auth_mode,
        availability=availability,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        active_lease_count=leases,
        reason_code=reason_code,
        reason=safe_executor_diagnostic_reason(reason_code),
    )

    assert diagnostic.reason_code == reason_code


@pytest.mark.parametrize(
    ("enabled", "auth_mode", "availability", "leases", "reason_code"),
    [
        # Reviewer reproduction: syntactically valid fields must not claim ready.
        (False, "unknown", ExecutorAvailability.AVAILABLE, None, "ready"),
        (True, "host_session", ExecutorAvailability.AVAILABLE, None, "ready"),
        (True, "unknown", ExecutorAvailability.AVAILABLE, 0, "ready"),
        (True, "host_session", ExecutorAvailability.UNKNOWN, 0, "inventory_unreconciled"),
        (False, "host_session", ExecutorAvailability.UNKNOWN, None, "disabled"),
        (True, "api_key", ExecutorAvailability.UNAVAILABLE, None, "local_auth_invalid"),
        (False, "unknown", ExecutorAvailability.UNKNOWN, None, "inventory_unreconciled"),
    ],
)
def test_diagnostic_contract_rejects_contradictory_semantic_states(
    enabled, auth_mode, availability, leases, reason_code
):
    from shared.contracts.dto.executor_diagnostics import safe_executor_diagnostic_reason

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ExecutorDiagnostic(
            executor=AgentType.CODEX,
            enabled=enabled,
            auth_mode=auth_mode,
            availability=availability,
            observed_at=now,
            expires_at=now + timedelta(seconds=60),
            active_lease_count=leases,
            reason_code=reason_code,
            reason=safe_executor_diagnostic_reason(reason_code),
        )


@pytest.mark.parametrize(
    ("availability", "reason_code"),
    [
        (ExecutorAvailability.AVAILABLE, "ready"),
        (ExecutorAvailability.DEGRADED, "local_warning"),
        (ExecutorAvailability.UNAVAILABLE, "local_auth_invalid"),
        (ExecutorAvailability.UNKNOWN, "inventory_unreconciled"),
    ],
)
def test_each_executor_availability_has_a_safe_contract(availability, reason_code):
    from shared.contracts.dto.executor_diagnostics import safe_executor_diagnostic_reason

    now = datetime.now(UTC)
    diagnostic = ExecutorDiagnostic(
        executor=AgentType.CODEX,
        enabled=True,
        auth_mode="host_session",
        availability=availability,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        active_lease_count=None if availability is ExecutorAvailability.UNKNOWN else 0,
        reason_code=reason_code,
        reason=safe_executor_diagnostic_reason(reason_code),
    )

    assert diagnostic.availability is availability


@pytest.mark.asyncio
async def test_redis_boundary_rejects_expired_and_partial_snapshots(monkeypatch):
    from src import dependencies

    now = datetime.now(UTC)
    item = {
        "executor": "codex",
        "enabled": True,
        "auth_mode": "host_session",
        "availability": "available",
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "active_lease_count": 0,
        "reason_code": "ready",
        "reason": "Local authentication and worker inventory are ready.",
    }
    expired = {
        "schema_version": "v1",
        "version": "expired",
        "observed_at": (now - timedelta(seconds=120)).isoformat(),
        "expires_at": (now - timedelta(seconds=60)).isoformat(),
        "diagnostics": [
            {
                **item,
                "observed_at": (now - timedelta(seconds=120)).isoformat(),
                "expires_at": (now - timedelta(seconds=60)).isoformat(),
            },
            {
                **item,
                "executor": "claude",
                "observed_at": (now - timedelta(seconds=120)).isoformat(),
                "expires_at": (now - timedelta(seconds=60)).isoformat(),
            },
        ],
    }
    redis = AsyncMock()
    monkeypatch.setattr(dependencies, "get_raw_redis", lambda: redis)
    redis.get.return_value = __import__("json").dumps(expired)
    assert await current_executor_snapshot() is None
    redis.get.return_value = __import__("json").dumps(
        {key: value for key, value in expired.items() if key != "schema_version"}
    )
    assert await current_executor_snapshot() is None


@pytest.mark.asyncio
async def test_semantically_contradictory_redis_snapshot_becomes_typed_unknown(monkeypatch):
    from src import dependencies

    now = datetime.now(UTC)
    item = {
        "executor": "codex",
        "enabled": False,
        "auth_mode": "unknown",
        "availability": "available",
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "active_lease_count": None,
        "reason_code": "ready",
        "reason": "Local authentication and worker inventory are ready.",
    }
    redis = AsyncMock()
    monkeypatch.setattr(dependencies, "get_raw_redis", lambda: redis)
    redis.get.return_value = __import__("json").dumps(
        {
            "schema_version": "v1",
            "version": "contradictory",
            "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=60)).isoformat(),
            "diagnostics": [item, {**item, "executor": "claude"}],
        }
    )

    diagnostic, snapshot = await current_executor_diagnostic(AgentType.CODEX)

    assert snapshot is None
    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.reason_code == "snapshot_unavailable"
