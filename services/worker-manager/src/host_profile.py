"""Passive, credential-free reading helpers for host-session profile readers.

The helpers turn locally stored metadata into typed instants. They never return,
log or raise with a token, a claim payload or a filesystem path.
"""

from __future__ import annotations

import base64
import binascii
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

_JWT_SEGMENTS = 3
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]*$")
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_LATEST = datetime(2200, 1, 1, tzinfo=UTC)
_FRACTION = re.compile(r"(\.\d{6})\d+")
#: A last-refresh stamp further ahead than this contradicts the local clock.
LAST_REFRESH_CLOCK_SKEW = timedelta(minutes=5)


class MetadataError(ValueError):
    """Stored time metadata is malformed or contradictory. Carries no detail."""


@dataclass(frozen=True)
class ProfileInspection:
    """One passive reading: the safe observation and a fixed refusal message.

    `refusal` is set exactly when the profile holds no usable refresh-capable
    material, so worker creation and diagnostics refuse the same profiles.
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


def _json_segment(segment: str) -> object | None:
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        return json.loads(decoded)
    except (binascii.Error, ValueError):
        return None


def jwt_expiry(token: str) -> datetime | None:
    """Return the `exp` of a structurally valid JWT, never its payload.

    A value is a JWT only when it has three base64url segments whose header is a
    JSON object naming `alg` and whose payload is a JSON object. Anything else is
    an opaque token and has no locally provable expiry (`None`). A JWT whose time
    claims are malformed or contradictory raises `MetadataError`. The signature
    is not verified: this is local metadata, not an authentication decision.
    """
    segments = token.split(".")
    if (
        len(segments) != _JWT_SEGMENTS
        or not _BASE64URL.match(segments[0])
        or not _BASE64URL.match(segments[1])
        or not _SIGNATURE.match(segments[2])
    ):
        return None
    header = _json_segment(segments[0])
    payload = _json_segment(segments[1])
    if (
        not isinstance(header, dict)
        or not isinstance(header.get("alg"), str)
        or not isinstance(payload, dict)
    ):
        return None
    if payload.get("exp") is None:
        return None
    expires_at = epoch_instant(payload["exp"])
    if payload.get("iat") is not None and epoch_instant(payload["iat"]) > expires_at:
        raise MetadataError
    return expires_at
