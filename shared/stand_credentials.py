"""Credential-safe token checks shared by the stand workflow and manager.

This module intentionally uses only the standard library. It accepts mappings
instead of process state so callers can inject a clock in tests and never need
to log, persist, or forward a credential value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

CLAUDE_MINIMUM_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class CredentialFailure:
    """One actionable and value-free credential refusal."""

    name: str
    detail: str


class CredentialShape(StrEnum):
    """Supported protected configurations which carry stand credentials."""

    PRECREATE_RUNNER = "precreate_runner"
    STAND_HOST = "stand_host"


@dataclass(frozen=True)
class StandTokenCredentials:
    """Canonical Claude credentials consumed only by local validation."""

    claude_token: str | None
    claude_expires_at: str | None


_CREDENTIAL_NAMES: dict[CredentialShape, tuple[str, str]] = {
    CredentialShape.PRECREATE_RUNNER: (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
    ),
    CredentialShape.STAND_HOST: (
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
    ),
}


def _credential_value(configuration: Mapping[str, str] | Any, name: str) -> str | None:
    if isinstance(configuration, Mapping):
        return configuration.get(name)
    return getattr(configuration, name, None)


def bind_stand_token_credentials(
    configuration: Mapping[str, str] | Any,
    *,
    shape: CredentialShape,
) -> StandTokenCredentials:
    """Normalize one caller configuration shape before value-free validation.

    The supported names are deliberately centralized here. Callers select a
    shape but must not translate individual credential names themselves.
    """
    claude_token, claude_expires_at = _CREDENTIAL_NAMES[shape]
    return StandTokenCredentials(
        claude_token=_credential_value(configuration, claude_token),
        claude_expires_at=_credential_value(configuration, claude_expires_at),
    )


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


def _claude_token_failure(
    *, credentials: StandTokenCredentials, now: datetime
) -> CredentialFailure | None:
    """Validate an opaque annual Claude token through its operator expiry metadata."""
    token = credentials.claude_token
    if not token or not token.strip():
        return CredentialFailure("Claude token", "is missing")
    expires_at = _operator_expiry(credentials.claude_expires_at)
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
    configuration: Mapping[str, str] | Any,
    *,
    shape: CredentialShape = CredentialShape.PRECREATE_RUNNER,
    now: datetime | None = None,
) -> list[CredentialFailure]:
    """Bind one caller shape and return value-free Claude token failures."""
    now = now or datetime.now(UTC)
    credentials = bind_stand_token_credentials(configuration, shape=shape)
    return [
        failure
        for failure in (_claude_token_failure(credentials=credentials, now=now),)
        if failure is not None
    ]
