from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json

import pytest

from scripts import stand_preflight


def _codex_token(expiry: datetime) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(expiry.timestamp())}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def _stand_environment(now: datetime) -> dict[str, str]:
    return {
        "STAND_CLAUDE_CODE_OAUTH_TOKEN": "fake-stand-claude-token",  # noqa: S105
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT": (now + timedelta(hours=1)).isoformat(),
        "STAND_CODEX_ACCESS_TOKEN": _codex_token(now + timedelta(hours=1)),
    }


@pytest.mark.parametrize(
    ("changed", "value", "expected"),
    [
        ("STAND_CLAUDE_CODE_OAUTH_TOKEN", "", "Claude token: is missing"),
        (
            "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
            "",
            "Claude token: has an unreadable or unverifiable expiry metadata",
        ),
        ("STAND_CODEX_ACCESS_TOKEN", "", "Codex token: is missing"),
        (
            "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
            "not-an-expiry",
            "Claude token: has an unreadable or unverifiable expiry metadata",
        ),
        (
            "STAND_CODEX_ACCESS_TOKEN",
            "fake-malformed-codex-token",
            "Codex token: has an unreadable or unverifiable expiry",
        ),
    ],
)
def test_stand_token_check_binds_real_stand_shaped_credentials_value_free(
    monkeypatch, changed, value, expected
):
    """The stand host uses STAND_* names, unlike the pre-create GitHub runner."""
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "CODEX_ACCESS_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "STAND_CODEX_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    environment = _stand_environment(datetime.now(UTC))
    environment[changed] = value
    for name, credential in environment.items():
        monkeypatch.setenv(name, credential)

    _, passed, detail = stand_preflight.check_stand_token_credentials()

    assert not passed
    assert detail == expected
    assert "fake-stand-claude-token" not in detail
    assert "fake-malformed-codex-token" not in detail


def test_stand_token_check_accepts_real_stand_shaped_credentials(monkeypatch):
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "CODEX_ACCESS_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "STAND_CODEX_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, credential in _stand_environment(datetime.now(UTC)).items():
        monkeypatch.setenv(name, credential)

    assert stand_preflight.check_stand_token_credentials() == (
        "stand token authentication",
        True,
        "local token prerequisites are valid",
    )


def test_stand_contour_uses_token_validation_not_host_session_paths(monkeypatch):
    checked: list[str] = []

    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setattr(stand_preflight, "check_contour", lambda: ("contour", True, "stand"))
    monkeypatch.setattr(stand_preflight, "check_docker", lambda: ("docker", True, ""))
    monkeypatch.setattr(stand_preflight, "check_disk", lambda: ("disk", True, ""))
    monkeypatch.setattr(stand_preflight, "check_deploy_target", lambda *_: ("deploy", True, ""))
    monkeypatch.setattr(
        stand_preflight,
        "check_stand_token_credentials",
        lambda: checked.append("token") or ("stand token", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_claude_session",
        lambda *_: checked.append("claude") or ("claude", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_codex_session",
        lambda *_: checked.append("codex") or ("codex", True, ""),
    )

    assert stand_preflight.main() == 0
    assert checked == ["token"]


def test_non_stand_contour_retains_the_host_session_checks(monkeypatch):
    checked: list[str] = []

    monkeypatch.delenv("LIVE_CONTOUR", raising=False)
    monkeypatch.setattr(stand_preflight, "check_contour", lambda: ("contour", True, ""))
    monkeypatch.setattr(stand_preflight, "check_docker", lambda: ("docker", True, ""))
    monkeypatch.setattr(stand_preflight, "check_disk", lambda: ("disk", True, ""))
    monkeypatch.setattr(stand_preflight, "check_deploy_target", lambda *_: ("deploy", True, ""))
    monkeypatch.setattr(
        stand_preflight,
        "check_stand_token_credentials",
        lambda: checked.append("token") or ("stand token", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_claude_session",
        lambda *_: checked.append("claude") or ("claude", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_codex_session",
        lambda *_: checked.append("codex") or ("codex", True, ""),
    )

    assert stand_preflight.main() == 0
    assert checked == ["claude", "codex"]
