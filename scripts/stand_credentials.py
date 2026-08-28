#!/usr/bin/env python3
"""Validate stand credentials before BitLaunch can create a machine.

This intentionally uses only the standard library. It checks the material the
workflow already has, never calls a provider and never renders credential
values. Provider account, quota and configured SSH-key checks remain the next
step in :mod:`scripts.stand_lifecycle`.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os

CLAUDE_MINIMUM_TTL = timedelta(minutes=30)
CODEX_MINIMUM_TTL = timedelta(minutes=30)
TELETHON_ENV_VARS = ("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION")
JWT_PART_COUNT = 3
PRIVATE_KEY_MINIMUM_LINES = 3


@dataclass(frozen=True)
class CredentialFailure:
    """One actionable and value-free pre-create refusal."""

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


def _ssh_private_key_looks_usable(value: str | None) -> bool:
    """Perform a local structural check without invoking ssh-keygen."""
    if not value:
        return False
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if (
        len(lines) < PRIVATE_KEY_MINIMUM_LINES
        or not lines[0].startswith("-----BEGIN ")
        or not lines[0].endswith("PRIVATE KEY-----")
    ):
        return False
    if lines[-1] != lines[0].replace("BEGIN", "END", 1):
        return False
    try:
        base64.b64decode("".join(lines[1:-1]), validate=True)
    except ValueError:
        return False
    return True


def validate_precreate_credentials(
    environment: Mapping[str, str], *, now: datetime | None = None
) -> list[CredentialFailure]:
    """Return every independent local refusal in workflow display order."""
    now = now or datetime.now(UTC)
    failures = [
        failure
        for failure in (
            _token_failure(
                name="Claude token",
                token=environment.get("CLAUDE_CODE_OAUTH_TOKEN"),
                minimum_ttl=CLAUDE_MINIMUM_TTL,
                now=now,
            ),
            _token_failure(
                name="Codex token",
                token=environment.get("CODEX_ACCESS_TOKEN"),
                minimum_ttl=CODEX_MINIMUM_TTL,
                now=now,
            ),
        )
        if failure is not None
    ]
    missing_telethon = [name for name in TELETHON_ENV_VARS if not environment.get(name, "").strip()]
    if missing_telethon:
        failures.append(
            CredentialFailure(
                "Telethon session", f"is unavailable: missing {', '.join(missing_telethon)}"
            )
        )
    if not _ssh_private_key_looks_usable(environment.get("SSH_PRIVATE_KEY")):
        failures.append(
            CredentialFailure("SSH material", "is missing or not a usable private-key document")
        )
    return failures


def main() -> int:
    failures = validate_precreate_credentials(os.environ)
    for failure in failures:
        print(f"FAIL {failure.name}: {failure.detail}")
    if failures:
        return 2
    print("ok  pre-create credentials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
