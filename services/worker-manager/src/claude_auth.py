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

from .claude_profile_v21278 import ClaudeProfileFormatError, session_material
from .host_profile import (
    JSON_PARSE_FAILURE,
    MAX_JSON_BYTES,
    ProfileFacts,
    ProfileInspection,
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

    The versioned adapter owns Claude Code's private credential shape. This
    reader owns stable file I/O, total JSON parsing and health classification.
    Claude's refresh token is opaque, so no refresh expiry is inferred.
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
    # The same total boundary as Codex: standard JSON (no NaN/Infinity literals)
    # within the shared size, depth and totality bounds. Claude Code is not a
    # serde_json reader, so Codex's pinned number-range, duplicate-key and
    # surrogate policies do not apply here.
    data = load_json(raw, pinned_serde_json=False)
    if data is JSON_PARSE_FAILURE:
        return unusable(_UNREADABLE)

    try:
        material = session_material(data)
    except ClaudeProfileFormatError:
        return unusable(_UNREADABLE)
    if material is None:
        return logged_out(_NOT_REFRESHABLE)

    facts = ProfileFacts()
    if material.metadata_invalid:
        facts = ProfileFacts(metadata_invalid=True)
    elif material.session_expires_at is not None:
        facts = ProfileFacts(
            session_expires_at=material.session_expires_at,
            session_expiry_source=CredentialExpirySource.CLAUDE_OAUTH_EXPIRES_AT,
        )

    if not material.refresh_token:
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
