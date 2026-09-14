"""Passive reader for the dedicated file-backed Codex host-session profile.

It reads `auth.json` and `config.toml` in place. It never runs the CLI,
contacts a provider, refreshes, copies or writes the profile.
"""

from datetime import UTC, datetime
import json
from pathlib import Path
import stat
import tomllib

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_PROFILE_EXPIRY_WARNING_WINDOW,
    CredentialExpirySource,
    ExecutorProfileCondition,
    LastRefreshSource,
    ProfileLoginState,
    RefreshMaterialState,
)

from .host_profile import (
    LAST_REFRESH_CLOCK_SKEW,
    MetadataError,
    ProfileFacts,
    ProfileInspection,
    iso_instant,
    jwt_expiry,
    logged_out,
    unusable,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_NOT_REFRESHABLE = "Codex auth.json does not contain a refresh-capable ChatGPT session"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def inspect_codex_host_session(profile_path: str | None, *, now: datetime) -> ProfileInspection:
    """Observe the ChatGPT session's login, refresh and time facts without exposing them.

    The access token's `exp` is reported as the session expiry. A refresh expiry
    is reported only when the refresh token is itself a structurally valid JWT;
    an opaque refresh token has none. `last_refresh` is reported as written.
    """
    if not profile_path:
        return unusable(
            "HOST_CODEX_HOME is required for Codex auth_mode=host_session; "
            "configure a dedicated profile created with codex login --device-auth"
        )
    profile = Path(profile_path)
    auth_data = _read_auth(profile)
    if isinstance(auth_data, ProfileInspection):
        return auth_data
    tokens = _session_tokens(auth_data)
    if isinstance(tokens, ProfileInspection):
        return tokens
    access_token, refresh_token = tokens

    facts = _facts(auth_data, access_token, refresh_token, now)
    if not refresh_token:
        return facts.refresh_missing(now, _NOT_REFRESHABLE)
    if not access_token:
        # Codex writes both tokens together; a lone refresh token is not its format.
        return unusable(_NOT_REFRESHABLE)
    config_refusal = _config_refusal(profile / "config.toml")
    if config_refusal is not None:
        return unusable(config_refusal)

    refresh_expires_at = facts.refresh_expires_at
    if refresh_expires_at is not None and refresh_expires_at <= now:
        return ProfileInspection(
            facts.observation(
                ExecutorProfileCondition.REFRESH_EXPIRED,
                ProfileLoginState.EXPIRED,
                RefreshMaterialState.PRESENT,
            ),
            "Codex host session refresh credential has expired",
        )
    if facts.metadata_invalid:
        condition, login_state = ExecutorProfileCondition.UNVERIFIABLE, ProfileLoginState.UNKNOWN
    elif (
        refresh_expires_at is not None
        and refresh_expires_at <= now + EXECUTOR_PROFILE_EXPIRY_WARNING_WINDOW
    ):
        condition, login_state = (
            ExecutorProfileCondition.REFRESH_EXPIRING,
            ProfileLoginState.LOGGED_IN,
        )
    else:
        condition, login_state = ExecutorProfileCondition.HEALTHY, ProfileLoginState.LOGGED_IN
    return ProfileInspection(
        facts.observation(condition, login_state, RefreshMaterialState.PRESENT), None
    )


def _read_auth(profile: Path) -> dict | ProfileInspection:
    """The parsed, non-empty `auth.json`, or the inspection that ends the read."""
    auth_path = profile / "auth.json"
    try:
        if not profile.is_dir():
            return unusable("HOST_CODEX_HOME is not an existing directory")
        if _mode(profile) != _PRIVATE_DIRECTORY_MODE:
            return unusable("HOST_CODEX_HOME must have mode 0700")
        if not auth_path.is_file() or auth_path.stat().st_size == 0:
            return logged_out("Codex host session is missing a non-empty auth.json")
        if _mode(auth_path) != _PRIVATE_FILE_MODE:
            return unusable("Codex auth.json must have mode 0600")
        auth_data = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        return unusable("Codex auth.json is unreadable or invalid JSON")
    if not isinstance(auth_data, dict):
        return unusable("Codex auth.json does not contain a cached session")
    if not auth_data:
        return logged_out("Codex auth.json does not contain a cached session")
    return auth_data


def _session_tokens(auth_data: dict) -> tuple[str | None, str | None] | ProfileInspection:
    """The stored access and refresh tokens, or the inspection that ends the read."""
    tokens = auth_data.get("tokens")
    if tokens is None or tokens == {}:
        return logged_out(_NOT_REFRESHABLE)
    if not isinstance(tokens, dict):
        return unusable(_NOT_REFRESHABLE)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if any(
        value is not None and not isinstance(value, str) for value in (access_token, refresh_token)
    ):
        return unusable(_NOT_REFRESHABLE)
    if not access_token and not refresh_token:
        return logged_out(_NOT_REFRESHABLE)
    return access_token, refresh_token


def _facts(
    auth_data: dict, access_token: str | None, refresh_token: str | None, now: datetime
) -> ProfileFacts:
    invalid = False
    session_expires_at = refresh_expires_at = last_refresh_at = None
    try:
        session_expires_at = jwt_expiry(access_token) if access_token else None
    except MetadataError:
        invalid = True
    try:
        refresh_expires_at = jwt_expiry(refresh_token) if refresh_token else None
    except MetadataError:
        invalid = True
    if auth_data.get("last_refresh") is not None:
        try:
            last_refresh_at = iso_instant(auth_data["last_refresh"])
            if last_refresh_at > now + LAST_REFRESH_CLOCK_SKEW:
                last_refresh_at = None
                raise MetadataError
        except MetadataError:
            invalid = True
    return ProfileFacts(
        session_expires_at=session_expires_at,
        session_expiry_source=(
            CredentialExpirySource.CODEX_ACCESS_TOKEN_JWT_EXP if session_expires_at else None
        ),
        refresh_expires_at=refresh_expires_at,
        refresh_expiry_source=(
            CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP if refresh_expires_at else None
        ),
        last_refresh_at=last_refresh_at,
        last_refresh_source=LastRefreshSource.CODEX_AUTH_LAST_REFRESH if last_refresh_at else None,
        metadata_invalid=invalid,
    )


def _config_refusal(config_path: Path) -> str | None:
    try:
        if not config_path.is_file():
            return "Codex host session is missing config.toml"
        if _mode(config_path) != _PRIVATE_FILE_MODE:
            return "Codex config.toml must have mode 0600"
        config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "Codex config.toml is unreadable or invalid TOML"
    if config.get("cli_auth_credentials_store") != "file":
        return 'Codex config.toml must set cli_auth_credentials_store = "file"'
    return None


def validate_codex_host_session(profile_path: str | None) -> None:
    """Refuse a profile without usable refresh material, as diagnostics report it."""
    inspect_codex_host_session(profile_path, now=datetime.now(UTC)).require_refresh_capable()
