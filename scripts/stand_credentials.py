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
from datetime import UTC, datetime
import os

from shared.stand_credentials import (
    CLAUDE_MINIMUM_TTL,  # noqa: F401 - retained script-level validator API
    CredentialFailure,
    CredentialShape,
    validate_stand_token_credentials,
)

TELETHON_ENV_VARS = ("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION")
PRIVATE_KEY_MINIMUM_LINES = 3


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
    failures = validate_stand_token_credentials(
        environment,
        shape=CredentialShape.PRECREATE_RUNNER,
        now=now,
    )
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
