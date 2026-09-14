"""Credential-safe availability facts for the two host-backed executors."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from shared.contracts.vocab import AgentType

# v2 replaced the released v1 snapshot without a compatibility reader: a v1
# value expires within its 90-second TTL and an API that finds no v2 value
# reports typed `unknown`, which is fail closed.
EXECUTOR_DIAGNOSTICS_REDIS_KEY = "executor:diagnostics:v2"
EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION = "v2"

#: A locally proved refresh expiry at or inside this window is degraded.
EXECUTOR_PROFILE_EXPIRY_WARNING_WINDOW = timedelta(hours=24)

# This is deliberately a closed mapping.  Redis is a shared transport, so a
# syntactically valid value is not enough to make text safe for an admin API.
SAFE_EXECUTOR_DIAGNOSTIC_REASONS = {
    "ready": "Local authentication and worker inventory are ready.",
    "disabled": "Host-session executor is not configured.",
    "local_auth_invalid": "Required local host-session material is unusable.",
    "profile_logged_out": "Host-session profile is logged out.",
    "profile_refresh_missing": "Host-session profile has no refresh credential.",
    "profile_refresh_expired": "Host-session refresh credential has expired.",
    "profile_refresh_expiring": "Host-session refresh credential expires within 24 hours.",
    "profile_metadata_unverifiable": "Host-session credential metadata could not be verified.",
    "api_key_missing": "Required local API-key configuration is unavailable.",
    "stand_token_ready": "Local stand-token authentication and worker inventory are ready.",
    "stand_token_invalid": "Required local stand-token authentication is unavailable.",
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
    STAND_TOKEN = "stand_token"  # noqa: S105 - non-secret wire enum value
    UNKNOWN = "unknown"


class ExecutorProfileCondition(StrEnum):
    """The one conclusion a passive read of a host-session profile supports."""

    HEALTHY = "healthy"
    REFRESH_EXPIRING = "refresh_expiring"
    REFRESH_EXPIRED = "refresh_expired"
    REFRESH_MISSING = "refresh_missing"
    LOGGED_OUT = "logged_out"
    #: The profile cannot be read or its shape/permissions/config are unusable.
    UNUSABLE = "unusable"
    #: Refresh material exists but stored time metadata is malformed or contradictory.
    UNVERIFIABLE = "unverifiable"


class ProfileLoginState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class RefreshMaterialState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    UNKNOWN = "unknown"


class CredentialExpirySource(StrEnum):
    """Where a stored expiry instant was read. Each names exactly one credential."""

    #: `claudeAiOauth.expiresAt`: the access-token (session) expiry Claude Code stores.
    CLAUDE_OAUTH_EXPIRES_AT = "claude_oauth_expires_at"
    #: `exp` of the Codex access token, read only when it is a structurally valid JWT.
    CODEX_ACCESS_TOKEN_JWT_EXP = "codex_access_token_jwt_exp"  # noqa: S105
    #: `exp` of the Codex refresh token, only when that token is itself a valid JWT.
    CODEX_REFRESH_TOKEN_JWT_EXP = "codex_refresh_token_jwt_exp"  # noqa: S105


class LastRefreshSource(StrEnum):
    #: The `last_refresh` timestamp Codex writes into `auth.json`.
    CODEX_AUTH_LAST_REFRESH = "codex_auth_last_refresh"


_SESSION_EXPIRY_SOURCES = {
    AgentType.CLAUDE: {CredentialExpirySource.CLAUDE_OAUTH_EXPIRES_AT},
    AgentType.CODEX: {CredentialExpirySource.CODEX_ACCESS_TOKEN_JWT_EXP},
}
_REFRESH_EXPIRY_SOURCES = {
    AgentType.CLAUDE: set(),
    AgentType.CODEX: {CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP},
}
_LAST_REFRESH_SOURCES = {
    AgentType.CLAUDE: set(),
    AgentType.CODEX: {LastRefreshSource.CODEX_AUTH_LAST_REFRESH},
}

#: The diagnostic reason and availability each profile condition asserts.
PROFILE_CONDITION_OUTCOMES: dict[ExecutorProfileCondition, tuple[str, ExecutorAvailability]] = {
    ExecutorProfileCondition.HEALTHY: ("ready", ExecutorAvailability.AVAILABLE),
    ExecutorProfileCondition.REFRESH_EXPIRING: (
        "profile_refresh_expiring",
        ExecutorAvailability.DEGRADED,
    ),
    ExecutorProfileCondition.REFRESH_EXPIRED: (
        "profile_refresh_expired",
        ExecutorAvailability.UNAVAILABLE,
    ),
    ExecutorProfileCondition.REFRESH_MISSING: (
        "profile_refresh_missing",
        ExecutorAvailability.UNAVAILABLE,
    ),
    ExecutorProfileCondition.LOGGED_OUT: ("profile_logged_out", ExecutorAvailability.UNAVAILABLE),
    ExecutorProfileCondition.UNUSABLE: ("local_auth_invalid", ExecutorAvailability.UNAVAILABLE),
    ExecutorProfileCondition.UNVERIFIABLE: (
        "profile_metadata_unverifiable",
        ExecutorAvailability.UNKNOWN,
    ),
}

#: Conditions whose admissibility still depends on a reconciled worker inventory.
INVENTORY_DEPENDENT_PROFILE_CONDITIONS = {
    ExecutorProfileCondition.HEALTHY,
    ExecutorProfileCondition.REFRESH_EXPIRING,
}


class ExecutorProfileObservation(BaseModel):
    """Credential-free facts from one passive read of a host-session profile.

    Access/session expiry and refresh-credential expiry are separate facts. An
    access token's `exp` never stands in for an opaque refresh token's expiry.
    """

    model_config = ConfigDict(extra="forbid")

    condition: ExecutorProfileCondition
    login_state: ProfileLoginState
    refresh_material: RefreshMaterialState
    session_expires_at: AwareDatetime | None = None
    session_expiry_source: CredentialExpirySource | None = None
    refresh_expires_at: AwareDatetime | None = None
    refresh_expiry_source: CredentialExpirySource | None = None
    last_refresh_at: AwareDatetime | None = None
    last_refresh_source: LastRefreshSource | None = None

    @model_validator(mode="after")
    def _consistent_facts(self) -> "ExecutorProfileObservation":
        for instant, source in (
            (self.session_expires_at, self.session_expiry_source),
            (self.refresh_expires_at, self.refresh_expiry_source),
            (self.last_refresh_at, self.last_refresh_source),
        ):
            if (instant is None) != (source is None):
                raise ValueError("a profile time fact requires exactly its source")
        has_facts = any(
            value is not None
            for value in (self.session_expires_at, self.refresh_expires_at, self.last_refresh_at)
        )
        condition = ExecutorProfileCondition
        login = ProfileLoginState
        refresh = RefreshMaterialState
        valid = {
            condition.HEALTHY: self.login_state is login.LOGGED_IN
            and self.refresh_material is refresh.PRESENT,
            condition.REFRESH_EXPIRING: self.login_state is login.LOGGED_IN
            and self.refresh_material is refresh.PRESENT
            and self.refresh_expires_at is not None,
            condition.REFRESH_EXPIRED: self.login_state is login.EXPIRED
            and self.refresh_material is refresh.PRESENT
            and self.refresh_expires_at is not None,
            condition.REFRESH_MISSING: self.refresh_material is refresh.MISSING
            and self.refresh_expires_at is None,
            condition.LOGGED_OUT: self.login_state is login.LOGGED_OUT
            and self.refresh_material is refresh.MISSING
            and not has_facts,
            condition.UNUSABLE: self.login_state is login.UNKNOWN
            and self.refresh_material is refresh.UNKNOWN
            and not has_facts,
            condition.UNVERIFIABLE: self.login_state is login.UNKNOWN
            and self.refresh_material is refresh.PRESENT,
        }[self.condition]
        if not valid:
            raise ValueError("profile facts contradict the profile condition")
        return self

    def validate_for(self, executor: AgentType, observed_at: datetime) -> None:
        """Check executor-specific sources and the condition's time window."""
        if (
            self.session_expiry_source is not None
            and self.session_expiry_source not in _SESSION_EXPIRY_SOURCES[executor]
        ):
            raise ValueError("session expiry source does not belong to this executor")
        if (
            self.refresh_expiry_source is not None
            and self.refresh_expiry_source not in _REFRESH_EXPIRY_SOURCES[executor]
        ):
            raise ValueError("refresh expiry source does not belong to this executor")
        if (
            self.last_refresh_source is not None
            and self.last_refresh_source not in _LAST_REFRESH_SOURCES[executor]
        ):
            raise ValueError("last-refresh source does not belong to this executor")
        warning_edge = observed_at + EXECUTOR_PROFILE_EXPIRY_WARNING_WINDOW
        refresh_at = self.refresh_expires_at
        condition = ExecutorProfileCondition
        if self.condition is condition.HEALTHY and refresh_at is not None:
            if refresh_at <= warning_edge:
                raise ValueError("a healthy profile cannot expire inside the warning window")
        if self.condition is condition.REFRESH_EXPIRING:
            assert refresh_at is not None  # noqa: S101 - enforced by _consistent_facts
            if not observed_at < refresh_at <= warning_edge:
                raise ValueError("an expiring profile must expire inside the warning window")
        if self.condition is condition.REFRESH_EXPIRED:
            assert refresh_at is not None  # noqa: S101 - enforced by _consistent_facts
            if refresh_at > observed_at:
                raise ValueError("an expired profile must have expired")
        if self.condition is condition.REFRESH_MISSING:
            session_expired = (
                self.session_expires_at is not None and self.session_expires_at <= observed_at
            )
            if self.login_state is ProfileLoginState.EXPIRED and not session_expired:
                raise ValueError("an expired login requires an expired session")
            if self.login_state is ProfileLoginState.LOGGED_IN and session_expired:
                raise ValueError("a logged-in profile cannot have an expired session")
            if self.login_state is ProfileLoginState.LOGGED_OUT:
                raise ValueError("a logged-out profile is not a missing-refresh profile")


class ExecutorDiagnostic(BaseModel):
    """One safe, locally observed executor fact. Never put credential detail here."""

    model_config = ConfigDict(extra="forbid")

    executor: Literal[AgentType.CLAUDE, AgentType.CODEX]
    enabled: StrictBool
    auth_mode: ExecutorAuthMode
    availability: ExecutorAvailability
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    active_lease_count: StrictInt | None = Field(default=None, ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: str = Field(min_length=1, max_length=280)
    #: Present exactly for an enabled host-session executor.
    profile: ExecutorProfileObservation | None = None

    @model_validator(mode="after")
    def _valid_window(self) -> "ExecutorDiagnostic":
        if self.expires_at <= self.observed_at:
            raise ValueError("diagnostic expiry must be after observation")
        if self.reason_code not in SAFE_EXECUTOR_DIAGNOSTIC_REASONS:
            raise ValueError("diagnostic reason code is not safe")
        if self.reason != safe_executor_diagnostic_reason(self.reason_code):
            raise ValueError("diagnostic reason must match its safe reason code")
        host_session = self.enabled and self.auth_mode is ExecutorAuthMode.HOST_SESSION
        if host_session != (self.profile is not None):
            raise ValueError("an enabled host-session diagnostic requires exactly one profile")
        if self.profile is not None:
            self.profile.validate_for(self.executor, self.observed_at)
            self._require_profile_outcome(self.profile)
            return self
        # A Redis snapshot is an untrusted boundary.  Each reason code carries
        # the complete state it is allowed to describe so a syntactically valid
        # record cannot turn contradictory configuration or inventory into an
        # availability claim at admission time.
        api_key = self.auth_mode is ExecutorAuthMode.API_KEY
        has_known_leases = self.active_lease_count is not None
        valid_state = {
            "ready": self.enabled
            and api_key
            and self.availability is ExecutorAvailability.AVAILABLE
            and has_known_leases,
            "local_warning": self.enabled
            and api_key
            and self.availability is ExecutorAvailability.DEGRADED
            and has_known_leases,
            "api_key_missing": self.enabled
            and api_key
            and self.availability is ExecutorAvailability.UNAVAILABLE
            and has_known_leases,
            "stand_token_ready": self.enabled
            and self.auth_mode is ExecutorAuthMode.STAND_TOKEN
            and self.availability is ExecutorAvailability.AVAILABLE
            and has_known_leases,
            "stand_token_invalid": self.enabled
            and self.auth_mode is ExecutorAuthMode.STAND_TOKEN
            and self.availability is ExecutorAvailability.UNAVAILABLE
            and has_known_leases,
            # Disabled is locally proven unavailable even if an inventory read
            # failed.  A reconciled live lease count remains exact rather than
            # being overwritten with zero.
            "disabled": not self.enabled
            and self.auth_mode in {ExecutorAuthMode.HOST_SESSION, ExecutorAuthMode.API_KEY}
            and self.availability is ExecutorAvailability.UNAVAILABLE,
            "inventory_unreconciled": self.enabled
            and (api_key or self.auth_mode is ExecutorAuthMode.STAND_TOKEN)
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
        }.get(self.reason_code, False)
        if not valid_state:
            raise ValueError("diagnostic fields contradict the reason-code state")
        return self

    def _require_profile_outcome(self, profile: ExecutorProfileObservation) -> None:
        """A host-session reason and availability are exactly what its profile proves."""
        reason_code, availability = PROFILE_CONDITION_OUTCOMES[profile.condition]
        if (
            self.active_lease_count is None
            and profile.condition in INVENTORY_DEPENDENT_PROFILE_CONDITIONS
        ):
            reason_code, availability = "inventory_unreconciled", ExecutorAvailability.UNKNOWN
        if self.reason_code != reason_code or self.availability is not availability:
            raise ValueError("diagnostic fields contradict the host-session profile")


class ExecutorDiagnosticSnapshot(BaseModel):
    """The all-or-nothing Redis handoff from worker-manager to the API."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v2"]
    version: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
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


# --- administrator alert episodes -----------------------------------------------------

#: One Redis value per executor; worker-manager's diagnostics publisher owns it.
EXECUTOR_PROFILE_ALERT_REDIS_KEY_PREFIX = "executor:profile-alert:v1:"


def executor_profile_alert_key(executor: AgentType) -> str:
    return f"{EXECUTOR_PROFILE_ALERT_REDIS_KEY_PREFIX}{executor.value}"


class ExecutorProfileAlertState(StrEnum):
    #: Delivery has not yet reached every configured administrator.
    OWED = "owed"
    #: Every configured administrator accepted this episode's alert.
    SETTLED = "settled"


class ExecutorProfileAlertOutcome(StrEnum):
    """The production `AdminDeliveryStatus` values, recorded per attempt."""

    DELIVERED = "delivered"
    PARTIAL = "partial"
    FAILED = "failed"
    UNADDRESSABLE = "unaddressable"


class ExecutorProfileAlertEpisode(BaseModel):
    """One alertable profile condition, deduplicated until a healthy observation."""

    model_config = ConfigDict(extra="forbid")

    executor: Literal[AgentType.CLAUDE, AgentType.CODEX]
    episode_id: str = Field(min_length=1, max_length=64)
    condition: ExecutorProfileCondition
    refresh_expires_at: AwareDatetime | None = None
    opened_at: AwareDatetime
    state: ExecutorProfileAlertState
    attempts: StrictInt = Field(ge=0)
    last_attempt_at: AwareDatetime | None = None
    last_outcome: ExecutorProfileAlertOutcome | None = None
    next_attempt_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent_delivery(self) -> "ExecutorProfileAlertEpisode":
        if self.condition is ExecutorProfileCondition.HEALTHY:
            raise ValueError("a healthy profile has no alert episode")
        if (self.attempts == 0) != (self.last_outcome is None) or (self.attempts == 0) != (
            self.last_attempt_at is None
        ):
            raise ValueError("delivery attempts, outcome and time must agree")
        if self.state is ExecutorProfileAlertState.SETTLED:
            if self.last_outcome is not ExecutorProfileAlertOutcome.DELIVERED:
                raise ValueError("only full delivery settles an alert episode")
            if self.next_attempt_at is not None:
                raise ValueError("a settled alert episode has no next attempt")
        elif self.next_attempt_at is None or (
            self.last_outcome is ExecutorProfileAlertOutcome.DELIVERED
        ):
            raise ValueError("an owed alert episode needs a next attempt and no full delivery")
        return self

    @property
    def reason_code(self) -> str:
        return PROFILE_CONDITION_OUTCOMES[self.condition][0]
