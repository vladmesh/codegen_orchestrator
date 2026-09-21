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

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
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

from .codex_profile_v01446 import (
    NOT_REFRESHABLE,
    auth_mode_refusal,
    format_refusal,
    parse_json,
    session_tokens,
)

from .host_profile import (
    JSON_PARSE_FAILURE,
    LAST_REFRESH_CLOCK_SKEW,
    MAX_JSON_BYTES,
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
#: The advisory lock `worker_wrapper.wrapper.codex_profile_lock` holds exclusively
#: for each whole Codex process sharing this profile.
CODEX_PROFILE_LOCK_NAME = ".codegen-codex.lock"
#: While a CLI holds the lock, this bounds the wait for one stable read (~0.25s).
STABLE_READ_ATTEMPTS = 5
STABLE_READ_PAUSE_SECONDS = 0.05


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
    format_refusal = format_refusal(auth_data)
    if format_refusal is not None:
        return unusable(format_refusal)
    mode_refusal = auth_mode_refusal(auth_data)
    if mode_refusal is not None:
        return unusable(mode_refusal)
    tokens = session_tokens(auth_data)
    if isinstance(tokens, ProfileInspection):
        return tokens
    access_token, refresh_token = tokens

    facts = _facts(auth_data, access_token, refresh_token, now)
    if not refresh_token:
        return facts.refresh_missing(now, NOT_REFRESHABLE)
    if not access_token:
        # Codex writes both tokens together; a lone refresh token is not its format.
        return unusable(NOT_REFRESHABLE)
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
        if before.st_size > MAX_JSON_BYTES:
            return unusable("Codex auth.json is unreadable or invalid JSON")
        # The CLI reads auth.json as a UTF-8 `String`; an undecodable file never loads.
        raw_auth = auth_path.read_text(encoding="utf-8")
        after = auth_path.stat()
    except (OSError, ValueError):
        return unusable("Codex auth.json is unreadable or invalid JSON")
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        return None
    auth_data = parse_json(raw_auth)
    if auth_data is JSON_PARSE_FAILURE:
        return unusable("Codex auth.json is unreadable or invalid JSON")
    if not isinstance(auth_data, dict):
        return unusable("Codex auth.json does not contain a cached session")
    if not auth_data:
        return logged_out("Codex auth.json does not contain a cached session")
    return auth_data


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
