"""Passive reader for the dedicated file-backed Codex host-session profile.

It reads `auth.json` and `config.toml` in place. It never runs the CLI,
contacts a provider, refreshes, copies or writes the profile.

One observation order serves worker creation and diagnostics alike:

1. Read `auth.json` stably. Join the worker wrapper's `.codegen-codex.lock`
   with a shared, non-blocking flock on a stable lock inode. Unless that lock is
   held, a writer may be active: a missing lock file never proves otherwise,
   because a wrapper can create and take it the next instant. Then accept only
   a read whose file identity did not change and whose JSON is a non-empty
   object, retrying within a short bound and otherwise reporting the
   non-alerting, non-refusing `read_contended`.
2. Require the file to deserialize as pinned Codex `AuthDotJson`/`TokenData`.
3. Require the CLI's authoritative ChatGPT `auth_mode`.
4. Only then interpret ChatGPT access/refresh material, time metadata and
   `config.toml`.
"""

import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
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
    read_contended,
    unusable,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_NOT_REFRESHABLE = "Codex auth.json does not contain a refresh-capable ChatGPT session"
_NOT_SUBSCRIPTION = "Codex auth.json is not in the ChatGPT subscription auth_mode"
_FORMAT_MISMATCH = "Codex auth.json does not match the pinned Codex CLI auth format"
#: Pinned Codex (rust-v0.144.6) `AuthMode` wire values; any other value fails to load.
_CLI_AUTH_MODES = frozenset(
    {
        "apikey",
        "chatgpt",
        "chatgptAuthTokens",
        "headers",
        "agentIdentity",
        "personalAccessToken",
        "bedrockApiKey",
    }
)
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
_BASE64URL_NO_PAD = re.compile(r"^[A-Za-z0-9_-]+$")
#: `header.payload.signature`; the pinned parser ignores any further segments.
_JWT_PARTS = 3
#: The advisory lock `worker_wrapper.wrapper.codex_profile_lock` holds exclusively
#: for each whole Codex process sharing this profile.
CODEX_PROFILE_LOCK_NAME = ".codegen-codex.lock"
#: While a CLI holds the lock, this bounds the wait for one stable read (~0.25s).
STABLE_READ_ATTEMPTS = 5
STABLE_READ_PAUSE_SECONDS = 0.05
_CHATGPT_AUTH_MODE = "chatgpt"
#: Stored credentials pinned Codex (rust-v0.144.6) `AuthDotJson::resolved_mode`
#: prefers over ChatGPT when `auth_mode` is absent.
_NON_CHATGPT_CREDENTIALS = ("personal_access_token", "bedrock_api_key", "OPENAI_API_KEY")


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
    format_refusal = _format_refusal(auth_data)
    if format_refusal is not None:
        return unusable(format_refusal)
    mode_refusal = _auth_mode_refusal(auth_data)
    if mode_refusal is not None:
        return unusable(mode_refusal)
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
    """Step 1: the parsed, non-empty `auth.json` from a stable read, or the end state."""
    try:
        if not profile.is_dir():
            return unusable("HOST_CODEX_HOME is not an existing directory")
        if _mode(profile) != _PRIVATE_DIRECTORY_MODE:
            return unusable("HOST_CODEX_HOME must have mode 0700")
    except OSError:
        return unusable("HOST_CODEX_HOME is not an existing directory")
    with _joined_profile_lock(profile) as uncontended:
        for attempt in range(STABLE_READ_ATTEMPTS):
            if attempt:
                time.sleep(STABLE_READ_PAUSE_SECONDS)
            outcome = _read_auth_once(profile / "auth.json")
            if isinstance(outcome, dict):
                return outcome
            # Under a held shared lock an empty or broken file is the real state.
            # Otherwise it may be the truncate-then-write window of a writer.
            if outcome is not None and uncontended:
                return outcome
    return read_contended()


@contextmanager
def _joined_profile_lock(profile: Path) -> Iterator[bool]:
    """Join the wrapper's profile lock read-only; yield whether no CLI can be writing.

    The pinned CLI rewrites `auth.json` in place (truncate, then write) and the
    wrapper holds this flock exclusively for the whole CLI process. Only a
    shared non-blocking lock on the inode the lock path still names proves no
    writer during the read; it is never waited for. A missing, unreadable, held
    or replaced lock proves nothing, so the read must be stable on its own.
    """
    descriptor = _acquire_shared_lock(profile / CODEX_PROFILE_LOCK_NAME)
    try:
        yield descriptor is not None
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _acquire_shared_lock(lock_path: Path) -> int | None:
    try:
        descriptor = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    try:
        stable = os.fstat(descriptor).st_ino == lock_path.stat().st_ino
    except OSError:
        stable = False
    if not stable:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        return None
    return descriptor


def _read_auth_once(auth_path: Path) -> dict | ProfileInspection | None:
    """One read bracketed by stats; `None` when the file changed underneath it."""
    try:
        if not auth_path.is_file():
            return logged_out("Codex host session is missing a non-empty auth.json")
        before = auth_path.stat()
        if before.st_size == 0:
            return logged_out("Codex host session is missing a non-empty auth.json")
        if stat.S_IMODE(before.st_mode) != _PRIVATE_FILE_MODE:
            return unusable("Codex auth.json must have mode 0600")
        raw_auth = auth_path.read_text()
        after = auth_path.stat()
    except OSError:
        return unusable("Codex auth.json is unreadable or invalid JSON")
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        return None
    try:
        auth_data = json.loads(raw_auth)
    except ValueError:
        return unusable("Codex auth.json is unreadable or invalid JSON")
    if not isinstance(auth_data, dict):
        return unusable("Codex auth.json does not contain a cached session")
    if not auth_data:
        return logged_out("Codex auth.json does not contain a cached session")
    return auth_data


def _format_refusal(auth_data: dict) -> str | None:
    """Step 2: the file loads as pinned Codex `AuthDotJson`, as the CLI would load it.

    Fields the CLI ignores stay ignored. Optional fields may be absent or null
    but must otherwise have their real types; present `tokens` must be a
    complete `TokenData` whose `id_token` the CLI can parse.
    """
    if not (
        _optional(auth_data, "auth_mode", lambda value: value in _CLI_AUTH_MODES)
        and _optional(auth_data, "OPENAI_API_KEY", _is_str)
        and _optional(auth_data, "personal_access_token", _is_str)
        and _optional(auth_data, "last_refresh", _is_rfc3339)
        # `AgentIdentityStorage` is a JWT string or a stored record object.
        and _optional(auth_data, "agent_identity", lambda value: isinstance(value, str | dict))
        and _optional(auth_data, "bedrock_api_key", _is_bedrock_api_key)
    ):
        return _FORMAT_MISMATCH
    tokens = auth_data.get("tokens")
    if tokens is None:
        return None
    if not isinstance(tokens, dict):
        return _FORMAT_MISMATCH
    if not isinstance(tokens.get("refresh_token"), str):
        return _NOT_REFRESHABLE
    if not (
        isinstance(tokens.get("access_token"), str)
        and _is_cli_id_token(tokens.get("id_token"))
        and _optional(tokens, "account_id", _is_str)
    ):
        return _FORMAT_MISMATCH
    return None


def _optional(data: dict, name: str, valid) -> bool:
    return data.get(name) is None or bool(valid(data[name]))


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or not _RFC3339.match(value):
        return False
    try:
        iso_instant(value)
    except MetadataError:
        return False
    return True


def _is_bedrock_api_key(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("api_key"), str)
        and isinstance(value.get("region"), str)
    )


def _is_cli_id_token(value: object) -> bool:
    """Mirror pinned `parse_chatgpt_jwt_claims`; only the claim types are checked."""
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    if (
        len(parts) < _JWT_PARTS
        or not all(parts[:_JWT_PARTS])
        or not _BASE64URL_NO_PAD.match(parts[1])
    ):
        return False
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return False
    if not isinstance(claims, dict):
        return False
    profile = claims.get("https://api.openai.com/profile")
    auth = claims.get("https://api.openai.com/auth")
    return (
        _optional(claims, "email", _is_str)
        and (
            profile is None or (isinstance(profile, dict) and _optional(profile, "email", _is_str))
        )
        and (auth is None or _is_auth_claims(auth))
    )


def _is_auth_claims(auth: object) -> bool:
    return (
        isinstance(auth, dict)
        and all(
            _optional(auth, name, _is_str)
            for name in ("chatgpt_plan_type", "chatgpt_user_id", "user_id", "chatgpt_account_id")
        )
        and (
            "chatgpt_account_is_fedramp" not in auth
            or isinstance(auth["chatgpt_account_is_fedramp"], bool)
        )
    )


def _auth_mode_refusal(auth_data: dict) -> str | None:
    """Step 3: the CLI's authoritative mode must be the file-backed ChatGPT session.

    An explicit `auth_mode` wins; when absent the CLI resolves a stored personal
    access token, Bedrock key or OpenAI API key before ChatGPT. Any other mode
    would run on that credential instead of the subscription, so retained
    ChatGPT tokens are never interpreted for it.
    """
    mode = auth_data.get("auth_mode")
    if mode is None:
        if any(auth_data.get(name) is not None for name in _NON_CHATGPT_CREDENTIALS):
            return _NOT_SUBSCRIPTION
        return None
    return None if mode == _CHATGPT_AUTH_MODE else _NOT_SUBSCRIPTION


def _session_tokens(auth_data: dict) -> tuple[str | None, str | None] | ProfileInspection:
    """Step 4: the stored ChatGPT access and refresh tokens, or the end state."""
    tokens = auth_data.get("tokens")
    if tokens is None:
        return logged_out(_NOT_REFRESHABLE)
    # Step 2 proved both are strings.
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
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
