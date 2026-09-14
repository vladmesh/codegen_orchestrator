"""Executor diagnostic snapshots fail closed at the API boundary."""

from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock

from pydantic import ValidationError
import pytest

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    CredentialExpirySource,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    ProfileLoginState,
    RefreshMaterialState,
    safe_executor_diagnostic_reason,
)
from shared.contracts.vocab import AgentType
from shared.tests.executor_diagnostic_cases import host_profile, host_profile_for_reason
from src.executor_diagnostics import current_executor_diagnostic, current_executor_snapshot

_HEALTHY = {"condition": "healthy", "login_state": "logged_in", "refresh_material": "present"}


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
        profile=host_profile(ExecutorProfileCondition.HEALTHY),
    )
    snapshot = ExecutorDiagnosticSnapshot(
        schema_version="v2",
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
            schema_version="v2",
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
                    profile=host_profile(ExecutorProfileCondition.HEALTHY),
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
                    profile=host_profile(ExecutorProfileCondition.HEALTHY),
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
        "profile": _HEALTHY,
    }
    with pytest.raises(ValidationError):
        ExecutorDiagnostic.model_validate(item)

    item["reason"] = "Local authentication and worker inventory are ready."
    ExecutorDiagnostic.model_validate(item)
    with pytest.raises(ValidationError):
        ExecutorDiagnosticSnapshot.model_validate(
            {
                "version": "opaque-version",
                "observed_at": now,
                "expires_at": now + timedelta(seconds=60),
                "diagnostics": [item, {**item, "executor": "claude"}],
            }
        )
    with pytest.raises(ValidationError):
        ExecutorDiagnosticSnapshot.model_validate(
            {
                "schema_version": "v1",
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
        (True, "api_key", ExecutorAvailability.AVAILABLE, 0, "ready"),
        (True, "api_key", ExecutorAvailability.DEGRADED, 1, "local_warning"),
        (True, "host_session", ExecutorAvailability.UNAVAILABLE, 2, "local_auth_invalid"),
        (True, "host_session", ExecutorAvailability.UNAVAILABLE, None, "local_auth_invalid"),
        (True, "host_session", ExecutorAvailability.UNAVAILABLE, 0, "profile_logged_out"),
        (True, "host_session", ExecutorAvailability.UNAVAILABLE, 0, "profile_refresh_missing"),
        (True, "host_session", ExecutorAvailability.UNKNOWN, 0, "profile_metadata_unverifiable"),
        (True, "api_key", ExecutorAvailability.UNAVAILABLE, 2, "api_key_missing"),
        (True, "stand_token", ExecutorAvailability.AVAILABLE, 0, "stand_token_ready"),
        (True, "stand_token", ExecutorAvailability.UNAVAILABLE, 2, "stand_token_invalid"),
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
        profile=(
            host_profile_for_reason(reason_code)
            if enabled and auth_mode == "host_session"
            else None
        ),
    )

    assert diagnostic.reason_code == reason_code


@pytest.mark.parametrize(
    ("enabled", "auth_mode", "availability", "leases", "reason_code", "condition"),
    [
        # Reviewer reproduction: syntactically valid fields must not claim ready.
        (False, "unknown", ExecutorAvailability.AVAILABLE, None, "ready", None),
        (True, "host_session", ExecutorAvailability.AVAILABLE, None, "ready", "healthy"),
        (True, "unknown", ExecutorAvailability.AVAILABLE, 0, "ready", None),
        (
            True,
            "host_session",
            ExecutorAvailability.UNKNOWN,
            0,
            "inventory_unreconciled",
            "healthy",
        ),
        (False, "host_session", ExecutorAvailability.UNKNOWN, None, "disabled", None),
        (True, "api_key", ExecutorAvailability.UNAVAILABLE, None, "local_auth_invalid", None),
        (False, "unknown", ExecutorAvailability.UNKNOWN, None, "inventory_unreconciled", None),
        # v2: a host-session claim is exactly what its profile proves.
        (True, "host_session", ExecutorAvailability.AVAILABLE, 0, "ready", None),
        (True, "host_session", ExecutorAvailability.AVAILABLE, 0, "ready", "logged_out"),
        (True, "host_session", ExecutorAvailability.AVAILABLE, 0, "ready", "unverifiable"),
        (
            True,
            "host_session",
            ExecutorAvailability.UNKNOWN,
            None,
            "inventory_unreconciled",
            "logged_out",
        ),
        (True, "host_session", ExecutorAvailability.DEGRADED, 0, "local_warning", "healthy"),
        (True, "stand_token", ExecutorAvailability.AVAILABLE, 0, "stand_token_ready", "healthy"),
        (False, "host_session", ExecutorAvailability.UNAVAILABLE, 0, "disabled", "healthy"),
    ],
)
def test_diagnostic_contract_rejects_contradictory_semantic_states(
    enabled, auth_mode, availability, leases, reason_code, condition
):
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
            profile=None
            if condition is None
            else host_profile(ExecutorProfileCondition(condition)),
        )


def _expiring_codex_profile(now: datetime) -> ExecutorProfileObservation:
    return ExecutorProfileObservation(
        condition=ExecutorProfileCondition.REFRESH_EXPIRING,
        login_state=ProfileLoginState.LOGGED_IN,
        refresh_material=RefreshMaterialState.PRESENT,
        refresh_expires_at=now + timedelta(hours=2),
        refresh_expiry_source=CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP,
    )


@pytest.mark.parametrize(
    ("availability", "reason_code"),
    [
        (ExecutorAvailability.AVAILABLE, "ready"),
        (ExecutorAvailability.DEGRADED, "profile_refresh_expiring"),
        (ExecutorAvailability.UNAVAILABLE, "local_auth_invalid"),
        (ExecutorAvailability.UNKNOWN, "profile_metadata_unverifiable"),
    ],
)
def test_each_executor_availability_has_a_safe_host_session_contract(availability, reason_code):
    now = datetime.now(UTC)
    profile = (
        _expiring_codex_profile(now)
        if reason_code == "profile_refresh_expiring"
        else host_profile_for_reason(reason_code)
    )
    diagnostic = ExecutorDiagnostic(
        executor=AgentType.CODEX,
        enabled=True,
        auth_mode="host_session",
        availability=availability,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        active_lease_count=0,
        reason_code=reason_code,
        reason=safe_executor_diagnostic_reason(reason_code),
        profile=profile,
    )

    assert diagnostic.availability is availability


def _snapshot_json(now: datetime, *, schema_version: str = "v2", **item_overrides) -> str:
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
        "profile": _HEALTHY,
        **item_overrides,
    }
    return json.dumps(
        {
            "schema_version": schema_version,
            "version": "stored",
            "observed_at": item["observed_at"],
            "expires_at": item["expires_at"],
            # Claude stays a plain ready entry, so each case exercises the Codex item.
            "diagnostics": [
                item,
                {
                    **item,
                    "executor": "claude",
                    "enabled": True,
                    "auth_mode": "host_session",
                    "availability": "available",
                    "active_lease_count": 0,
                    "reason_code": "ready",
                    "reason": safe_executor_diagnostic_reason("ready"),
                    "profile": _HEALTHY,
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_redis_boundary_reads_the_v2_key_and_rejects_expired_partial_and_v1(monkeypatch):
    from src import dependencies

    now = datetime.now(UTC)
    redis = AsyncMock()
    monkeypatch.setattr(dependencies, "get_raw_redis", lambda: redis)

    redis.get.return_value = _snapshot_json(now)
    snapshot = await current_executor_snapshot()
    assert snapshot is not None
    assert snapshot.for_executor(AgentType.CODEX, now).profile is not None
    assert (
        redis.get.await_args.args[0] == EXECUTOR_DIAGNOSTICS_REDIS_KEY == "executor:diagnostics:v2"
    )

    redis.get.return_value = _snapshot_json(now - timedelta(seconds=120))
    assert await current_executor_snapshot() is None
    partial = json.loads(_snapshot_json(now))
    del partial["schema_version"]
    redis.get.return_value = json.dumps(partial)
    assert await current_executor_snapshot() is None
    # A released v1 value (no profile, schema v1) is never read as current.
    v1 = json.loads(_snapshot_json(now, schema_version="v1"))
    for item in v1["diagnostics"]:
        del item["profile"]
    redis.get.return_value = json.dumps(v1)
    diagnostic, stored = await current_executor_diagnostic(AgentType.CODEX)
    assert stored is None
    assert diagnostic.availability is ExecutorAvailability.UNKNOWN


@pytest.mark.asyncio
async def test_a_bad_profile_cannot_be_stored_as_available(monkeypatch):
    from src import dependencies

    now = datetime.now(UTC)
    redis = AsyncMock()
    monkeypatch.setattr(dependencies, "get_raw_redis", lambda: redis)
    redis.get.return_value = _snapshot_json(
        now,
        profile={
            "condition": "logged_out",
            "login_state": "logged_out",
            "refresh_material": "missing",
        },
    )

    diagnostic, snapshot = await current_executor_diagnostic(AgentType.CODEX)

    assert snapshot is None
    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.reason_code == "snapshot_unavailable"


@pytest.mark.parametrize(
    "profile",
    [
        # A credential fragment cannot ride along in an extra field.
        {**_HEALTHY, "refresh_token": "rt-not-safe"},
        # Naive timestamps are not accepted.
        {
            **_HEALTHY,
            "session_expires_at": "2030-01-01T00:00:00",
            "session_expiry_source": "codex_access_token_jwt_exp",
        },
        # An access-token exp never proves a refresh expiry.
        {
            **_HEALTHY,
            "refresh_expires_at": "2030-01-01T00:00:00Z",
            "refresh_expiry_source": "codex_access_token_jwt_exp",
        },
        # A source without its instant, and a Claude source on Codex.
        {**_HEALTHY, "session_expiry_source": "codex_access_token_jwt_exp"},
        {
            **_HEALTHY,
            "session_expires_at": "2030-01-01T00:00:00Z",
            "session_expiry_source": "claude_oauth_expires_at",
        },
    ],
)
def test_profile_contract_rejects_unsafe_or_unprovable_facts(profile):
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ExecutorDiagnosticSnapshot.model_validate_json(_snapshot_json(now, profile=profile))


@pytest.mark.parametrize(
    ("condition", "refresh_offset", "valid"),
    [
        ("refresh_expiring", timedelta(hours=24), True),
        ("refresh_expiring", timedelta(hours=24, seconds=1), False),
        ("healthy", timedelta(hours=24, seconds=1), True),
        ("healthy", timedelta(hours=24), False),
        ("refresh_expired", timedelta(0), True),
        ("refresh_expiring", timedelta(0), False),
    ],
)
def test_profile_contract_enforces_the_24_hour_boundary(condition, refresh_offset, valid):
    now = datetime.now(UTC)
    reason_code, availability = {
        "healthy": ("ready", "available"),
        "refresh_expiring": ("profile_refresh_expiring", "degraded"),
        "refresh_expired": ("profile_refresh_expired", "unavailable"),
    }[condition]
    profile = {
        "condition": condition,
        "login_state": "expired" if condition == "refresh_expired" else "logged_in",
        "refresh_material": "present",
        "refresh_expires_at": (now + refresh_offset).isoformat(),
        "refresh_expiry_source": "codex_refresh_token_jwt_exp",
    }
    raw = _snapshot_json(
        now,
        availability=availability,
        reason_code=reason_code,
        reason=safe_executor_diagnostic_reason(reason_code),
        profile=profile,
    )
    if valid:
        ExecutorDiagnosticSnapshot.model_validate_json(raw)
    else:
        with pytest.raises(ValidationError):
            ExecutorDiagnosticSnapshot.model_validate_json(raw)


@pytest.mark.asyncio
async def test_semantically_contradictory_redis_snapshot_becomes_typed_unknown(monkeypatch):
    from src import dependencies

    now = datetime.now(UTC)
    redis = AsyncMock()
    monkeypatch.setattr(dependencies, "get_raw_redis", lambda: redis)
    redis.get.return_value = _snapshot_json(
        now, enabled=False, auth_mode="unknown", active_lease_count=None, profile=None
    )

    diagnostic, snapshot = await current_executor_diagnostic(AgentType.CODEX)

    assert snapshot is None
    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.reason_code == "snapshot_unavailable"


@pytest.mark.asyncio
async def test_endpoint_fallback_is_a_v2_unknown_snapshot_without_profiles(monkeypatch):
    from src.routers.work_admission import get_executor_diagnostics

    async def no_snapshot():
        return None

    monkeypatch.setattr("src.routers.work_admission.current_executor_snapshot", no_snapshot)

    snapshot = await get_executor_diagnostics(None)

    assert snapshot.schema_version == "v2"
    assert {item.availability for item in snapshot.diagnostics} == {ExecutorAvailability.UNKNOWN}
    assert all(item.profile is None for item in snapshot.diagnostics)
