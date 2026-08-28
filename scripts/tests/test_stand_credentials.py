from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json

from scripts.stand_credentials import (
    CLAUDE_MINIMUM_TTL,
    CODEX_MINIMUM_TTL,
    validate_precreate_credentials,
)


def _token(expiry: datetime) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(expiry.timestamp())}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def _environment(now: datetime) -> dict[str, str]:
    return {
        "CLAUDE_CODE_OAUTH_TOKEN": _token(now + CLAUDE_MINIMUM_TTL + timedelta(seconds=1)),
        "CODEX_ACCESS_TOKEN": _token(now + CODEX_MINIMUM_TTL + timedelta(seconds=1)),
        "TELETHON_API_ID": "12345",
        "TELETHON_API_HASH": "test-hash",
        "TELETHON_SESSION": "test-session",
        "SSH_PRIVATE_KEY": (
            "-----BEGIN OPENSSH PRIVATE KEY-----\ndGVzdA==\n-----END OPENSSH PRIVATE KEY-----"
        ),
    }


def test_precreate_credentials_accepts_only_tokens_above_each_explicit_ttl():
    now = datetime(2026, 8, 28, tzinfo=UTC)

    assert validate_precreate_credentials(_environment(now), now=now) == []


def test_precreate_credentials_refuses_each_independent_missing_or_unusable_input():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    environment = _environment(now)
    environment.update(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CODEX_ACCESS_TOKEN": _token(now + CODEX_MINIMUM_TTL),
            "TELETHON_SESSION": "",
            "SSH_PRIVATE_KEY": "not-a-private-key",
        }
    )

    failures = validate_precreate_credentials(environment, now=now)

    assert [failure.name for failure in failures] == [
        "Claude token",
        "Codex token",
        "Telethon session",
        "SSH material",
    ]
    assert all(
        "token" not in failure.detail.lower()
        or "missing" in failure.detail.lower()
        or "expired" in failure.detail.lower()
        or "minimum" in failure.detail.lower()
        for failure in failures
    )


def test_precreate_credentials_refuses_malformed_and_expired_tokens_without_echoing_them():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    environment = _environment(now)
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = "fake-malformed-claude-token"  # noqa: S105
    environment["CODEX_ACCESS_TOKEN"] = _token(now - timedelta(seconds=1))

    failures = validate_precreate_credentials(environment, now=now)
    rendered = "\n".join(failure.detail for failure in failures)

    assert [failure.name for failure in failures] == ["Claude token", "Codex token"]
    assert "fake-malformed-claude-token" not in rendered
    assert "header." not in rendered
