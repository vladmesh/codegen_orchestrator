"""Credential-safe availability facts for the two host-backed executors."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from shared.contracts.vocab import AgentType

EXECUTOR_DIAGNOSTICS_REDIS_KEY = "executor:diagnostics:v1"
EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION = "v1"


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
    active_lease_count: StrictInt = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: str = Field(min_length=1, max_length=280)

    @model_validator(mode="after")
    def _valid_window(self) -> "ExecutorDiagnostic":
        if self.expires_at <= self.observed_at:
            raise ValueError("diagnostic expiry must be after observation")
        return self


class ExecutorDiagnosticSnapshot(BaseModel):
    """The all-or-nothing Redis handoff from worker-manager to the API."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION
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
