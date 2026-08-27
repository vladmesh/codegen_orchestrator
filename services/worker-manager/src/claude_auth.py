"""Fail-fast validation for the dedicated Claude Code host-session profile."""

import json
from pathlib import Path


def validate_claude_host_session(profile_path: str | None) -> None:
    """Validate local refresh material without refreshing it or exposing it."""
    if not profile_path:
        raise RuntimeError("HOST_CLAUDE_DIR is required for Claude auth_mode=host_session")
    credentials = Path(profile_path) / ".credentials.json"
    if not credentials.is_file() or credentials.stat().st_size == 0:
        raise RuntimeError("Claude host session is missing credentials")
    try:
        data = json.loads(credentials.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Claude host session credentials are unreadable") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("refreshToken"), str) or not oauth["refreshToken"]:
        raise RuntimeError("Claude host session has no refresh-capable credentials")
