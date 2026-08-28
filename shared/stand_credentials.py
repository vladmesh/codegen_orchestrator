"""Credential-safe token checks shared by the stand workflow and manager.

This module intentionally uses only the standard library. It accepts mappings
instead of process state so callers can inject a clock in tests and never need
to log, persist, or forward a credential value.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json

CLAUDE_MINIMUM_TTL = timedelta(minutes=30)
CODEX_MINIMUM_TTL = timedelta(minutes=30)
JWT_PART_COUNT = 3


@dataclass(frozen=True)
class CredentialFailure:
    """One actionable and value-free credential refusal."""

    name: str
    detail: str


def _jwt_expiry(token: str) -> datetime | None:
    """Return a JWT expiry or None when it cannot be independently verified."""
    parts = token.split(".")
    if len(parts) != JWT_PART_COUNT or not parts[1]:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        expiry = payload["exp"]
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            return None
        return datetime.fromtimestamp(expiry, UTC)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _operator_expiry(value: str | None) -> datetime | None:
    """Parse the Claude operator expiry metadata as an aware ISO-8601 instant."""
    if not value or not value.strip():
        return None
    try:
        expiry = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return None
    return expiry.astimezone(UTC)


def _token_failure(
    *, name: str, token: str | None, minimum_ttl: timedelta, now: datetime
) -> CredentialFailure | None:
    if not token or not token.strip():
        return CredentialFailure(name, "is missing")
    expires_at = _jwt_expiry(token.strip())
    if expires_at is None:
        return CredentialFailure(name, "has an unreadable or unverifiable expiry")
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return CredentialFailure(name, "is expired")
    if remaining <= minimum_ttl:
        return CredentialFailure(
            name,
            "has less than the required "
            f"{int(minimum_ttl.total_seconds() // 60)} minutes remaining",
        )
    return None


def _claude_token_failure(
    *, environment: Mapping[str, str], now: datetime
) -> CredentialFailure | None:
    """Validate an opaque annual Claude token through its operator expiry metadata."""
    token = environment.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token or not token.strip():
        return CredentialFailure("Claude token", "is missing")
    expires_at = _operator_expiry(environment.get("CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT"))
    if expires_at is None:
        return CredentialFailure(
            "Claude token", "has an unreadable or unverifiable expiry metadata"
        )
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return CredentialFailure("Claude token", "is expired")
    if remaining <= CLAUDE_MINIMUM_TTL:
        return CredentialFailure(
            "Claude token",
            "has less than the required "
            f"{int(CLAUDE_MINIMUM_TTL.total_seconds() // 60)} minutes remaining",
        )
    return None


def validate_stand_token_credentials(
    environment: Mapping[str, str], *, now: datetime | None = None
) -> list[CredentialFailure]:
    """Return value-free Claude and Codex token failures for stand mode."""
    now = now or datetime.now(UTC)
    return [
        failure
        for failure in (
            _claude_token_failure(environment=environment, now=now),
            _token_failure(
                name="Codex token",
                token=environment.get("CODEX_ACCESS_TOKEN"),
                minimum_ttl=CODEX_MINIMUM_TTL,
                now=now,
            ),
        )
        if failure is not None
    ]
