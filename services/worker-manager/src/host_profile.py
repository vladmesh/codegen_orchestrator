"""Passive, credential-free reading helpers for host-session profile readers.

The helpers turn locally stored metadata into typed instants. They never return,
log or raise with a token, a claim payload or a filesystem path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
import re

from shared.contracts.dto.executor_diagnostics import (
    CredentialExpirySource,
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    LastRefreshSource,
    ProfileLoginState,
    RefreshMaterialState,
)

_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_LATEST = datetime(2200, 1, 1, tzinfo=UTC)
_FRACTION = re.compile(r"(\.\d{6})\d+")

#: Profile files are a few kilobytes; the bound keeps every diagnostics tick bounded.
MAX_JSON_BYTES = 1 << 20
#: Shared defensive limit for profile JSON. Versioned adapters may impose the
#: same or a stricter parser-specific bound when mirroring a vendor parser.
MAX_JSON_NESTING = 127


class _JsonParseFailure:
    """The one failure result of `load_json`; never equal to a parsed JSON value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "JSON_PARSE_FAILURE"


JSON_PARSE_FAILURE = _JsonParseFailure()


class _JsonRejected(ValueError):
    """A JSON document the boundary refuses. Carries no document content."""


def load_json(raw: str | bytes) -> object:
    """The total, vendor-neutral standard-JSON boundary for host profile readers.

    The shared layer bounds input size and nesting, decodes strict UTF-8, and
    refuses Python's non-standard NaN/Infinity literals. Vendor-specific parser
    behavior belongs in a versioned profile adapter. Returns the parsed value or
    `JSON_PARSE_FAILURE`; it never raises.
    """
    try:
        if isinstance(raw, bytes):
            if len(raw) > MAX_JSON_BYTES:
                return JSON_PARSE_FAILURE
            text = raw.decode("utf-8")
        else:
            if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
                return JSON_PARSE_FAILURE
            text = raw
        if _nesting_exceeds(text, MAX_JSON_NESTING):
            return JSON_PARSE_FAILURE
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError, TypeError, OverflowError):
        return JSON_PARSE_FAILURE
    return value


def _nesting_exceeds(text: str, limit: int) -> bool:
    """Count container nesting outside strings without recursing."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


def _reject_json_constant(_name: str) -> object:
    raise _JsonRejected("standard JSON has no NaN, Infinity or -Infinity literal")


#: A last-refresh stamp further ahead than this contradicts the local clock.
LAST_REFRESH_CLOCK_SKEW = timedelta(minutes=5)


class MetadataError(ValueError):
    """Stored time metadata is malformed or contradictory. Carries no detail."""


@dataclass(frozen=True)
class ProfileInspection:
    """One passive reading: the safe observation and a fixed refusal message.

    `refusal` is set exactly when the profile holds no usable refresh-capable
    material, so worker creation and diagnostics refuse the same profiles. A
    contended or unverifiable read proves neither way and carries no refusal.
    """

    observation: ExecutorProfileObservation
    refusal: str | None

    def require_refresh_capable(self) -> None:
        if self.refusal is not None:
            raise RuntimeError(self.refusal)


def unusable(refusal: str) -> ProfileInspection:
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.UNUSABLE,
            login_state=ProfileLoginState.UNKNOWN,
            refresh_material=RefreshMaterialState.UNKNOWN,
        ),
        refusal,
    )


def read_contended() -> ProfileInspection:
    """A CLI was rewriting the profile and no stable read completed in the bound."""
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.READ_CONTENDED,
            login_state=ProfileLoginState.UNKNOWN,
            refresh_material=RefreshMaterialState.UNKNOWN,
        ),
        None,
    )


def logged_out(refusal: str) -> ProfileInspection:
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.LOGGED_OUT,
            login_state=ProfileLoginState.LOGGED_OUT,
            refresh_material=RefreshMaterialState.MISSING,
        ),
        refusal,
    )


@dataclass(frozen=True)
class ProfileFacts:
    """Parsed, credential-free time facts and whether any stored value was bad."""

    session_expires_at: datetime | None = None
    session_expiry_source: CredentialExpirySource | None = None
    refresh_expires_at: datetime | None = None
    refresh_expiry_source: CredentialExpirySource | None = None
    last_refresh_at: datetime | None = None
    last_refresh_source: LastRefreshSource | None = None
    metadata_invalid: bool = False

    def observation(
        self,
        condition: ExecutorProfileCondition,
        login_state: ProfileLoginState,
        refresh_material: RefreshMaterialState,
    ) -> ExecutorProfileObservation:
        return ExecutorProfileObservation(
            condition=condition,
            login_state=login_state,
            refresh_material=refresh_material,
            session_expires_at=self.session_expires_at,
            session_expiry_source=self.session_expiry_source,
            refresh_expires_at=self.refresh_expires_at,
            refresh_expiry_source=self.refresh_expiry_source,
            last_refresh_at=self.last_refresh_at,
            last_refresh_source=self.last_refresh_source,
        )

    def refresh_missing(self, now: datetime, refusal: str) -> ProfileInspection:
        """Session material without a refresh credential cannot be renewed."""
        if self.metadata_invalid:
            login_state = ProfileLoginState.UNKNOWN
        elif self.session_expires_at is not None and self.session_expires_at <= now:
            login_state = ProfileLoginState.EXPIRED
        else:
            login_state = ProfileLoginState.LOGGED_IN
        return ProfileInspection(
            self.observation(
                ExecutorProfileCondition.REFRESH_MISSING,
                login_state,
                RefreshMaterialState.MISSING,
            ),
            refusal,
        )


def epoch_instant(value: object, *, milliseconds: bool = False) -> datetime:
    """A timezone-aware UTC instant from a stored numeric epoch."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MetadataError
    if not math.isfinite(value):
        raise MetadataError
    seconds = value / 1000 if milliseconds else value
    try:
        instant = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise MetadataError from exc
    if not _EARLIEST <= instant < _LATEST:
        raise MetadataError
    return instant


def iso_instant(value: object) -> datetime:
    """A timezone-aware UTC instant from a stored ISO-8601 string; naive is invalid."""
    if not isinstance(value, str):
        raise MetadataError
    try:
        # Rust writers keep nanoseconds; Python keeps microseconds.
        instant = datetime.fromisoformat(_FRACTION.sub(r"\1", value))
    except ValueError as exc:
        raise MetadataError from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise MetadataError
    instant = instant.astimezone(UTC)
    if not _EARLIEST <= instant < _LATEST:
        raise MetadataError
    return instant
