"""Credential-safe availability facts for the two host-backed executors."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from shared.contracts.vocab import AgentType

EXECUTOR_DIAGNOSTICS_REDIS_KEY = "executor:diagnostics:v1"
EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION = "v1"

# This is deliberately a closed mapping.  Redis is a shared transport, so a
# syntactically valid value is not enough to make text safe for an admin API.
SAFE_EXECUTOR_DIAGNOSTIC_REASONS = {
    "ready": "Local authentication and worker inventory are ready.",
    "disabled": "Host-session executor is not configured.",
    "local_auth_invalid": "Required local host-session material is unusable.",
    "api_key_missing": "Required local API-key configuration is unavailable.",
    "local_warning": "Local authentication is usable with a non-fatal warning.",
    "inventory_unreconciled": "Worker inventory could not be reconciled.",
    "snapshot_unavailable": "Current executor diagnostics are unavailable.",
    "snapshot_expired": "Current executor diagnostics have expired.",
}


def safe_executor_diagnostic_reason(reason_code: str) -> str:
    """Return the sole response text allowed for a diagnostic reason code."""
    return SAFE_EXECUTOR_DIAGNOSTIC_REASONS[reason_code]


class ExecutorAvailability(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ExecutorAuthMode(StrEnum):
    HOST_SESSION = "host_session"
    API_KEY = "api_key"
    UNKNOWN = "unknown"


class ExecutorDiagnostic(BaseModel):
    """One safe, locally observed executor fact. Never put credential detail here."""

    model_config = ConfigDict(extra="forbid")

    executor: Literal[AgentType.CLAUDE, AgentType.CODEX]
    enabled: StrictBool
    auth_mode: ExecutorAuthMode
    availability: ExecutorAvailability
    observed_at: datetime
    expires_at: datetime
    active_lease_count: StrictInt | None = Field(default=None, ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: str = Field(min_length=1, max_length=280)

    @model_validator(mode="after")
    def _valid_window(self) -> "ExecutorDiagnostic":
        if self.expires_at <= self.observed_at:
            raise ValueError("diagnostic expiry must be after observation")
        if self.reason_code not in SAFE_EXECUTOR_DIAGNOSTIC_REASONS:
            raise ValueError("diagnostic reason code is not safe")
        if self.reason != safe_executor_diagnostic_reason(self.reason_code):
            raise ValueError("diagnostic reason must match its safe reason code")
        # A Redis snapshot is an untrusted boundary.  Each reason code carries
        # the complete state it is allowed to describe so a syntactically valid
        # record cannot turn contradictory configuration or inventory into an
        # availability claim at admission time.
        local_auth_mode = self.auth_mode in {
            ExecutorAuthMode.HOST_SESSION,
            ExecutorAuthMode.API_KEY,
        }
        has_known_leases = self.active_lease_count is not None
        valid_state = {
            "ready": self.enabled
            and local_auth_mode
            and self.availability is ExecutorAvailability.AVAILABLE
            and has_known_leases,
            "local_warning": self.enabled
            and local_auth_mode
            and self.availability is ExecutorAvailability.DEGRADED
            and has_known_leases,
            "local_auth_invalid": self.enabled
            and self.auth_mode is ExecutorAuthMode.HOST_SESSION
            and self.availability is ExecutorAvailability.UNAVAILABLE
            and has_known_leases,
            "api_key_missing": self.enabled
            and self.auth_mode is ExecutorAuthMode.API_KEY
            and self.availability is ExecutorAvailability.UNAVAILABLE
            and has_known_leases,
            # Disabled is locally proven unavailable even if an inventory read
            # failed.  A reconciled live lease count remains exact rather than
            # being overwritten with zero.
            "disabled": not self.enabled
            and local_auth_mode
            and self.availability is ExecutorAvailability.UNAVAILABLE,
            "inventory_unreconciled": self.enabled
            and local_auth_mode
            and self.availability is ExecutorAvailability.UNKNOWN
            and not has_known_leases,
            # These are API boundary fallbacks, never producer observations.
            "snapshot_unavailable": not self.enabled
            and self.auth_mode is ExecutorAuthMode.UNKNOWN
            and self.availability is ExecutorAvailability.UNKNOWN
            and not has_known_leases,
            "snapshot_expired": not self.enabled
            and self.auth_mode is ExecutorAuthMode.UNKNOWN
            and self.availability is ExecutorAvailability.UNKNOWN
            and not has_known_leases,
        }[self.reason_code]
        if not valid_state:
            raise ValueError("diagnostic fields contradict the reason-code state")
        return self


class ExecutorDiagnosticSnapshot(BaseModel):
    """The all-or-nothing Redis handoff from worker-manager to the API."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"]
    version: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    expires_at: datetime
    diagnostics: list[ExecutorDiagnostic] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _complete_snapshot(self) -> "ExecutorDiagnosticSnapshot":
        expected = {AgentType.CLAUDE, AgentType.CODEX}
        if {item.executor for item in self.diagnostics} != expected:
            raise ValueError("snapshot must contain exactly Claude and Codex")
        if self.expires_at <= self.observed_at:
            raise ValueError("snapshot expiry must be after observation")
        if any(
            item.observed_at != self.observed_at or item.expires_at != self.expires_at
            for item in self.diagnostics
        ):
            raise ValueError("executor diagnostic windows must match the snapshot window")
        return self

    def for_executor(self, executor: AgentType, now: datetime) -> ExecutorDiagnostic:
        if executor not in {AgentType.CLAUDE, AgentType.CODEX}:
            raise ValueError(f"{executor.value} has no executor diagnostic")
        if self.expires_at <= now:
            raise ValueError("snapshot is expired")
        item = next(item for item in self.diagnostics if item.executor is executor)
        if item.expires_at <= now:
            raise ValueError("executor diagnostic is expired")
        return item
