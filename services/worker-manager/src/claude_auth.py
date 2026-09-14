"""Passive reader for the dedicated Claude Code host-session profile.

It reads `.credentials.json` in place. It never runs the CLI, contacts a
provider, refreshes, copies or writes the profile.
"""

from datetime import UTC, datetime
from pathlib import Path

from shared.contracts.dto.executor_diagnostics import (
    CredentialExpirySource,
    ExecutorProfileCondition,
    ProfileLoginState,
    RefreshMaterialState,
)

from .host_profile import (
    JSON_PARSE_FAILURE,
    MAX_JSON_BYTES,
    MetadataError,
    ProfileFacts,
    ProfileInspection,
    epoch_instant,
    load_json,
    logged_out,
    unusable,
)

_REQUIRED = "HOST_CLAUDE_DIR is required for Claude auth_mode=host_session"
_MISSING = "Claude host session is missing credentials"
_UNREADABLE = "Claude host session credentials are unreadable"
_NOT_REFRESHABLE = "Claude host session has no refresh-capable credentials"


def inspect_claude_host_session(profile_path: str | None, *, now: datetime) -> ProfileInspection:
    """Observe login, refresh material and stored session expiry without exposing them.

    Claude Code stores `claudeAiOauth.expiresAt` (epoch milliseconds) for the
    access token only. Its refresh token is opaque, so no refresh expiry is ever
    reported for Claude and an expired access token with a refresh token present
    is still a renewable login.
    """
    if not profile_path:
        return unusable(_REQUIRED)
    credentials = Path(profile_path) / ".credentials.json"
    try:
        if not credentials.is_file() or credentials.stat().st_size == 0:
            return logged_out(_MISSING)
        if credentials.stat().st_size > MAX_JSON_BYTES:
            return unusable(_UNREADABLE)
        raw = credentials.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return unusable(_UNREADABLE)
    # The same total boundary as Codex, with Python JSON semantics: Claude Code
    # is not a serde_json reader, so only the bounds and totality are shared.
    data = load_json(raw, pinned_serde_json=False)
    if data is JSON_PARSE_FAILURE or not isinstance(data, dict):
        return unusable(_UNREADABLE)

    oauth = data.get("claudeAiOauth")
    if oauth is None or oauth == {}:
        return logged_out(_NOT_REFRESHABLE)
    if not isinstance(oauth, dict):
        return unusable(_UNREADABLE)
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if any(
        value is not None and not isinstance(value, str) for value in (access_token, refresh_token)
    ):
        return unusable(_UNREADABLE)
    if not access_token and not refresh_token:
        return logged_out(_NOT_REFRESHABLE)

    facts = ProfileFacts()
    if oauth.get("expiresAt") is not None:
        try:
            facts = ProfileFacts(
                session_expires_at=epoch_instant(oauth["expiresAt"], milliseconds=True),
                session_expiry_source=CredentialExpirySource.CLAUDE_OAUTH_EXPIRES_AT,
            )
        except MetadataError:
            facts = ProfileFacts(metadata_invalid=True)

    if not refresh_token:
        return facts.refresh_missing(now, _NOT_REFRESHABLE)
    if facts.metadata_invalid:
        return ProfileInspection(
            facts.observation(
                ExecutorProfileCondition.UNVERIFIABLE,
                ProfileLoginState.UNKNOWN,
                RefreshMaterialState.PRESENT,
            ),
            None,
        )
    return ProfileInspection(
        facts.observation(
            ExecutorProfileCondition.HEALTHY,
            ProfileLoginState.LOGGED_IN,
            RefreshMaterialState.PRESENT,
        ),
        None,
    )


def validate_claude_host_session(profile_path: str | None) -> None:
    """Refuse a profile without usable refresh material, as diagnostics report it."""
    inspect_claude_host_session(profile_path, now=datetime.now(UTC)).require_refresh_capable()
