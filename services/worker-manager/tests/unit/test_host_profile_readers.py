"""Passive, credential-safe host-session profile readers.

Every profile here is synthetic. No test authenticates a CLI or reads a real
profile; the passivity tests make any subprocess, copy, network or Docker call
fail loudly.
"""

import asyncio
import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest

from shared.contracts.dto.executor_diagnostics import (
    CredentialExpirySource,
    ExecutorAvailability,
    ExecutorProfileCondition,
    LastRefreshSource,
    ProfileLoginState,
    RefreshMaterialState,
)
from shared.contracts.vocab import AgentType
from src.claude_auth import inspect_claude_host_session, validate_claude_host_session
from src.codex_auth import inspect_codex_host_session, validate_codex_host_session
from src.host_profile import jwt_expiry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
ROOT_DIR = Path(__file__).resolve().parents[4]
ACCESS_SECRET = "sk-ant-oat01-SYNTHETIC-ACCESS"  # noqa: S105 - synthetic fixture
REFRESH_SECRET = "sk-ant-ort01-SYNTHETIC-REFRESH"  # noqa: S105 - synthetic fixture
OPAQUE_CODEX_REFRESH = "rt_SYNTHETIC-opaque-refresh"  # noqa: S105 - synthetic fixture


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(**claims) -> str:
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64({'sub': 'synthetic', **claims})}.c2ln"


def _epoch(instant: datetime) -> int:
    return int(instant.timestamp())


# --- Claude -------------------------------------------------------------------------


def _claude_profile(
    tmp_path: Path, content: object | None = None, *, raw: str | None = None
) -> Path:
    profile = tmp_path / "claude-profile"
    profile.mkdir(parents=True)
    if raw is not None:
        (profile / ".credentials.json").write_text(raw)
    elif content is not None:
        (profile / ".credentials.json").write_text(json.dumps(content))
    return profile


def _claude_oauth(**overrides) -> dict:
    oauth = {
        "accessToken": ACCESS_SECRET,
        "refreshToken": REFRESH_SECRET,
        "expiresAt": _epoch(NOW + timedelta(hours=8)) * 1000,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
    oauth.update(overrides)
    return {"claudeAiOauth": {key: value for key, value in oauth.items() if value is not ...}}


def test_claude_valid_profile_reports_session_expiry_but_never_a_refresh_expiry(tmp_path):
    inspection = inspect_claude_host_session(
        str(_claude_profile(tmp_path, _claude_oauth())), now=NOW
    )

    observation = inspection.observation
    assert inspection.refusal is None
    assert observation.condition is ExecutorProfileCondition.HEALTHY
    assert observation.login_state is ProfileLoginState.LOGGED_IN
    assert observation.refresh_material is RefreshMaterialState.PRESENT
    assert observation.session_expires_at == NOW + timedelta(hours=8)
    assert observation.session_expires_at.tzinfo is not None
    assert observation.session_expiry_source is CredentialExpirySource.CLAUDE_OAUTH_EXPIRES_AT
    assert observation.refresh_expires_at is None
    assert observation.last_refresh_at is None


def test_claude_expired_access_token_with_refresh_material_is_a_renewable_login(tmp_path):
    content = _claude_oauth(expiresAt=_epoch(NOW - timedelta(days=3)) * 1000)

    inspection = inspect_claude_host_session(str(_claude_profile(tmp_path, content)), now=NOW)

    assert inspection.refusal is None
    assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY
    assert inspection.observation.session_expires_at == NOW - timedelta(days=3)


@pytest.mark.parametrize(
    ("content", "raw"),
    [
        (None, None),  # no credentials file
        (None, ""),  # empty credentials file
        ({}, None),  # no OAuth object
        ({"claudeAiOauth": None}, None),
        ({"claudeAiOauth": {}}, None),
        ({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}, None),
    ],
)
def test_claude_logged_out_profiles(tmp_path, content, raw):
    inspection = inspect_claude_host_session(
        str(_claude_profile(tmp_path, content, raw=raw)), now=NOW
    )

    assert inspection.observation.condition is ExecutorProfileCondition.LOGGED_OUT
    assert inspection.observation.login_state is ProfileLoginState.LOGGED_OUT
    assert inspection.observation.refresh_material is RefreshMaterialState.MISSING
    assert inspection.refusal is not None


@pytest.mark.parametrize("refresh", [..., "", None])
def test_claude_missing_refresh_material_is_unavailable(tmp_path, refresh):
    content = _claude_oauth(refreshToken=refresh)

    inspection = inspect_claude_host_session(str(_claude_profile(tmp_path, content)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.REFRESH_MISSING
    assert inspection.observation.refresh_material is RefreshMaterialState.MISSING
    assert inspection.refusal == "Claude host session has no refresh-capable credentials"
    with pytest.raises(RuntimeError, match="refresh-capable"):
        validate_claude_host_session(str(_claude_profile(tmp_path / "again", content)))


def test_claude_missing_refresh_with_expired_session_is_an_expired_login(tmp_path):
    content = _claude_oauth(refreshToken=..., expiresAt=_epoch(NOW - timedelta(minutes=1)) * 1000)

    inspection = inspect_claude_host_session(str(_claude_profile(tmp_path, content)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.REFRESH_MISSING
    assert inspection.observation.login_state is ProfileLoginState.EXPIRED


@pytest.mark.parametrize(
    ("content", "raw"),
    [
        (None, "{not json"),
        ([], None),
        ({"claudeAiOauth": "token"}, None),
        ({"claudeAiOauth": {"refreshToken": 42}}, None),
    ],
)
def test_claude_malformed_profiles_are_unusable(tmp_path, content, raw):
    inspection = inspect_claude_host_session(
        str(_claude_profile(tmp_path, content, raw=raw)), now=NOW
    )

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert inspection.refusal == "Claude host session credentials are unreadable"


def test_claude_unreadable_profile_is_unusable(tmp_path, monkeypatch):
    profile = _claude_profile(tmp_path, _claude_oauth())

    def unreadable(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)

    inspection = inspect_claude_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert str(profile) not in (inspection.refusal or "")


@pytest.mark.parametrize("expires_at", ["tomorrow", True, -1, 10**30, float("nan")])
def test_claude_malformed_expiry_metadata_is_unverifiable_not_healthy(tmp_path, expires_at):
    content = _claude_oauth(expiresAt=expires_at)
    # json.dumps writes NaN literally, which json.loads reads back as a float.
    inspection = inspect_claude_host_session(str(_claude_profile(tmp_path, content)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNVERIFIABLE
    assert inspection.observation.login_state is ProfileLoginState.UNKNOWN
    assert inspection.observation.session_expires_at is None
    # Refresh material exists, so worker creation does not disagree with diagnostics.
    assert inspection.refusal is None


def test_claude_requires_a_configured_profile():
    inspection = inspect_claude_host_session(None, now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    with pytest.raises(RuntimeError, match="HOST_CLAUDE_DIR"):
        validate_claude_host_session(None)


# --- Codex --------------------------------------------------------------------------


def _codex_profile(
    tmp_path: Path, auth: object | None = None, *, raw: str | None = None, config: str | None = None
) -> Path:
    profile = tmp_path / "codex-profile"
    profile.mkdir(parents=True, mode=0o700)
    profile.chmod(0o700)
    auth_path = profile / "auth.json"
    if raw is not None:
        auth_path.write_text(raw)
    elif auth is not None:
        auth_path.write_text(json.dumps(auth))
    if auth_path.exists():
        auth_path.chmod(0o600)
    config_path = profile / "config.toml"
    config_path.write_text(
        config if config is not None else 'cli_auth_credentials_store = "file"\n'
    )
    config_path.chmod(0o600)
    # A bootstrapped profile: the login recipe and every Codex worker create it.
    lock_path = profile / ".codegen-codex.lock"
    lock_path.touch()
    lock_path.chmod(0o600)
    return profile


def _codex_auth(*, access=..., refresh=..., last_refresh=...) -> dict:
    tokens = {
        "id_token": _jwt(exp=_epoch(NOW + timedelta(hours=1))),
        "access_token": _jwt(
            iat=_epoch(NOW - timedelta(days=1)), exp=_epoch(NOW + timedelta(days=9))
        )
        if access is ...
        else access,
        "refresh_token": OPAQUE_CODEX_REFRESH if refresh is ... else refresh,
        "account_id": "synthetic-account",
    }
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {key: value for key, value in tokens.items() if value is not None},
        "last_refresh": "2026-09-13T08:30:00.123456789Z" if last_refresh is ... else last_refresh,
    }
    return {
        key: value for key, value in auth.items() if value is not None or key == "OPENAI_API_KEY"
    }


def test_codex_valid_profile_reports_access_expiry_and_last_refresh_but_no_opaque_refresh_expiry(
    tmp_path,
):
    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, _codex_auth())), now=NOW)

    observation = inspection.observation
    assert inspection.refusal is None
    assert observation.condition is ExecutorProfileCondition.HEALTHY
    assert observation.login_state is ProfileLoginState.LOGGED_IN
    assert observation.session_expires_at == NOW + timedelta(days=9)
    assert observation.session_expiry_source is CredentialExpirySource.CODEX_ACCESS_TOKEN_JWT_EXP
    # The refresh token is opaque: its expiry is not inferred from the access token.
    assert observation.refresh_expires_at is None
    assert observation.refresh_expiry_source is None
    assert observation.last_refresh_at == datetime(2026, 9, 13, 8, 30, 0, 123456, tzinfo=UTC)
    assert observation.last_refresh_source is LastRefreshSource.CODEX_AUTH_LAST_REFRESH


def test_codex_absent_last_refresh_is_not_invented(tmp_path):
    auth = _codex_auth(last_refresh=None)

    observation = inspect_codex_host_session(
        str(_codex_profile(tmp_path, auth)), now=NOW
    ).observation

    assert observation.condition is ExecutorProfileCondition.HEALTHY
    assert observation.last_refresh_at is None
    assert observation.last_refresh_source is None


def test_codex_opaque_access_token_has_no_session_expiry(tmp_path):
    auth = _codex_auth(access="opaque.access.token")

    observation = inspect_codex_host_session(
        str(_codex_profile(tmp_path, auth)), now=NOW
    ).observation

    assert observation.condition is ExecutorProfileCondition.HEALTHY
    assert observation.session_expires_at is None


@pytest.mark.parametrize(
    ("offset", "condition", "refusal"),
    [
        (timedelta(hours=24, seconds=1), ExecutorProfileCondition.HEALTHY, None),
        (timedelta(hours=24), ExecutorProfileCondition.REFRESH_EXPIRING, None),
        (timedelta(hours=3), ExecutorProfileCondition.REFRESH_EXPIRING, None),
        (timedelta(seconds=1), ExecutorProfileCondition.REFRESH_EXPIRING, None),
        (timedelta(0), ExecutorProfileCondition.REFRESH_EXPIRED, "refresh credential has expired"),
        (
            -timedelta(days=1),
            ExecutorProfileCondition.REFRESH_EXPIRED,
            "refresh credential has expired",
        ),
    ],
)
def test_codex_jwt_refresh_expiry_uses_the_24_hour_boundary(tmp_path, offset, condition, refusal):
    auth = _codex_auth(refresh=_jwt(exp=_epoch(NOW + offset)))

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is condition
    assert inspection.observation.refresh_expires_at == NOW + offset
    assert (
        inspection.observation.refresh_expiry_source
        is CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP
    )
    if refusal is None:
        assert inspection.refusal is None
    else:
        assert refusal in inspection.refusal
        assert inspection.observation.login_state is ProfileLoginState.EXPIRED


@pytest.mark.parametrize(
    ("auth", "raw"),
    [
        (None, None),  # auth.json removed by logout
        (None, ""),
        ({}, None),
        ({"OPENAI_API_KEY": None}, None),
        ({"auth_mode": "chatgpt", "tokens": None}, None),
        ({"tokens": {"id_token": _jwt(), "access_token": "", "refresh_token": ""}}, None),
    ],
)
def test_codex_logged_out_profiles(tmp_path, auth, raw):
    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth, raw=raw)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.LOGGED_OUT
    assert inspection.refusal is not None


def test_codex_empty_refresh_token_is_unavailable(tmp_path):
    auth = _codex_auth(refresh="")

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.REFRESH_MISSING
    assert "refresh-capable" in inspection.refusal


def test_codex_absent_refresh_token_field_is_a_file_the_cli_cannot_load(tmp_path):
    auth = _codex_auth(refresh=None)

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert "refresh-capable" in inspection.refusal


@pytest.mark.parametrize(
    ("auth", "raw", "config"),
    [
        (None, "{not json", None),
        ([], None, None),
        ({"tokens": "token"}, None, None),
        ({"tokens": {"access_token": 1, "refresh_token": "x"}}, None, None),
        ({"tokens": {"refresh_token": OPAQUE_CODEX_REFRESH}}, None, None),
        # Required TokenData fields are missing, so the pinned CLI cannot load these.
        ({"tokens": {}}, None, None),
        ({"tokens": {"access_token": "", "refresh_token": ""}}, None, None),
        (..., None, 'cli_auth_credentials_store = "keyring"\n'),
        (..., None, "not = [valid"),
    ],
)
def test_codex_malformed_profiles_are_unusable(tmp_path, auth, raw, config):
    auth = _codex_auth() if auth is ... else auth
    inspection = inspect_codex_host_session(
        str(_codex_profile(tmp_path, auth, raw=raw, config=config)), now=NOW
    )

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert inspection.refusal is not None


def test_codex_unreadable_profile_is_unusable(tmp_path, monkeypatch):
    profile = _codex_profile(tmp_path, _codex_auth())

    def unreadable(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE


@pytest.mark.parametrize(
    "auth_kwargs",
    [
        {"access": _jwt(exp="soon")},
        {"access": _jwt(exp=True)},
        {"access": _jwt(iat=_epoch(NOW), exp=_epoch(NOW - timedelta(days=1)))},
        {"refresh": _jwt(exp=[1])},
        {"last_refresh": (NOW + timedelta(hours=1)).isoformat()},  # ahead of the clock
    ],
)
def test_codex_malformed_or_contradictory_metadata_is_unverifiable(tmp_path, auth_kwargs):
    inspection = inspect_codex_host_session(
        str(_codex_profile(tmp_path, _codex_auth(**auth_kwargs))), now=NOW
    )

    assert inspection.observation.condition is ExecutorProfileCondition.UNVERIFIABLE
    assert inspection.observation.login_state is ProfileLoginState.UNKNOWN
    assert inspection.refusal is None


def test_jwt_expiry_reads_claims_only_from_structurally_valid_jwts():
    exp = _epoch(NOW)

    assert jwt_expiry(_jwt(exp=exp)) == NOW
    assert jwt_expiry(_jwt()) is None
    assert jwt_expiry("rt_opaque") is None
    assert jwt_expiry("aaaa.bbbb.cccc") is None  # not base64 JSON
    assert jwt_expiry(f"{_b64({'typ': 'JWT'})}.{_b64({'exp': exp})}.sig") is None  # no alg
    assert jwt_expiry(f"{_b64({'alg': 'none'})}.{_b64({'exp': exp})}.") == NOW


# --- passivity and redaction --------------------------------------------------------


def _forbid_side_effects(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a passive profile read performed a forbidden side effect")

    for target, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
        (os, "system"),
        (os, "execv"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (shutil, "copy"),
        (shutil, "copy2"),
        (shutil, "copyfile"),
        (shutil, "copytree"),
        (socket.socket, "connect"),
        (Path, "write_text"),
        (Path, "write_bytes"),
        (Path, "touch"),
        (Path, "replace"),
        (Path, "rename"),
        (Path, "unlink"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    import docker
    import httpx

    real_os_open = os.open

    def read_only_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("a passive profile read opened a file for writing")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only_open)
    monkeypatch.setattr(docker, "from_env", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def _tree_state(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "case",
    ["claude_valid", "claude_logged_out", "codex_valid", "codex_expiring", "codex_malformed"],
)
def test_readers_and_the_diagnostic_are_passive_and_in_place(tmp_path, monkeypatch, case):
    from unittest.mock import AsyncMock, MagicMock

    import src.executor_diagnostics as diagnostics_module

    if case.startswith("claude"):
        content = _claude_oauth() if case == "claude_valid" else {"claudeAiOauth": {}}
        profile = _claude_profile(tmp_path, content)
        executor = AgentType.CLAUDE
        monkeypatch.setattr(diagnostics_module.settings, "HOST_CLAUDE_DIR", "/docker-host/.claude")
        monkeypatch.setattr(
            diagnostics_module.settings, "HOST_CLAUDE_VALIDATION_PATH", str(profile)
        )
    else:
        auth = {
            "codex_valid": _codex_auth(),
            "codex_expiring": _codex_auth(refresh=_jwt(exp=_epoch(NOW + timedelta(hours=2)))),
            "codex_malformed": _codex_auth(last_refresh="naive-or-bad"),
        }[case]
        profile = _codex_profile(tmp_path, auth)
        executor = AgentType.CODEX
        monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/docker-host/.codex")
        monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", None, raising=False)
    before = _tree_state(tmp_path)
    siblings_before = sorted(path.name for path in tmp_path.iterdir())
    _forbid_side_effects(monkeypatch)

    inspect = (
        inspect_claude_host_session if executor is AgentType.CLAUDE else inspect_codex_host_session
    )
    inspection = inspect(str(profile), now=NOW)
    diagnostic = diagnostics_module.ExecutorDiagnostics(
        redis=AsyncMock(), docker=MagicMock(), alerts=MagicMock()
    )._executor_diagnostic(
        executor, NOW, NOW + timedelta(seconds=90), {AgentType.CLAUDE: 0, AgentType.CODEX: 0}
    )

    assert _tree_state(tmp_path) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == siblings_before
    serialized = diagnostic.model_dump_json() + inspection.observation.model_dump_json()
    for fragment in (
        ACCESS_SECRET,
        REFRESH_SECRET,
        OPAQUE_CODEX_REFRESH,
        "synthetic-account",
        str(tmp_path),
        "/docker-host",
        "eyJ",  # any base64 JSON (JWT) segment
    ):
        assert fragment not in serialized
        assert fragment not in (inspection.refusal or "")
    assert diagnostic.profile == inspection.observation


def test_reader_sources_use_no_process_copy_network_or_write_primitives():
    root = Path(__file__).resolve().parents[2] / "src"
    for name in ("claude_auth.py", "codex_auth.py", "host_profile.py"):
        # The one descriptor a reader opens: the worker lock, read-only, to join it.
        source = (root / name).read_text().replace("os.open(lock_path, os.O_RDONLY)", "")
        for forbidden in (
            "O_WRONLY",
            "O_RDWR",
            "O_CREAT",
            "O_TRUNC",
            "LOCK_EX",
            "subprocess",
            "shutil",
            "socket",
            "httpx",
            "docker",
            "write_text",
            "write_bytes",
            "open(",
            "os.system",
            "create_subprocess",
        ):
            assert forbidden not in source, f"{name} uses {forbidden}"


def test_diagnostics_map_each_profile_condition_to_the_admission_outcome(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import src.executor_diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", None, raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/docker-host/.codex")
    diagnostics = diagnostics_module.ExecutorDiagnostics(
        redis=AsyncMock(), docker=MagicMock(), alerts=MagicMock()
    )
    now = datetime.now(UTC)
    cases = {
        "healthy": (_codex_auth(), ExecutorAvailability.AVAILABLE, "ready"),
        "expiring": (
            _codex_auth(refresh=_jwt(exp=_epoch(now + timedelta(hours=5)))),
            ExecutorAvailability.DEGRADED,
            "profile_refresh_expiring",
        ),
        "expired": (
            _codex_auth(refresh=_jwt(exp=_epoch(now - timedelta(hours=5)))),
            ExecutorAvailability.UNAVAILABLE,
            "profile_refresh_expired",
        ),
        "logged_out": (
            {"auth_mode": "chatgpt"},
            ExecutorAvailability.UNAVAILABLE,
            "profile_logged_out",
        ),
        "refresh_missing": (
            _codex_auth(refresh=""),
            ExecutorAvailability.UNAVAILABLE,
            "profile_refresh_missing",
        ),
        "unverifiable": (
            _codex_auth(last_refresh=(now + timedelta(hours=1)).isoformat()),
            ExecutorAvailability.UNKNOWN,
            "profile_metadata_unverifiable",
        ),
    }
    for name, (auth, availability, reason_code) in cases.items():
        profile = _codex_profile(tmp_path / name, auth)
        monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))

        diagnostic = diagnostics._executor_diagnostic(
            AgentType.CODEX,
            now,
            now + timedelta(seconds=90),
            {AgentType.CLAUDE: 0, AgentType.CODEX: 1},
        )
        unreconciled = diagnostics._executor_diagnostic(
            AgentType.CODEX, now, now + timedelta(seconds=90), None
        )

        assert (diagnostic.availability, diagnostic.reason_code) == (availability, reason_code), (
            name
        )
        assert diagnostic.active_lease_count == 1
        if availability in {ExecutorAvailability.AVAILABLE, ExecutorAvailability.DEGRADED}:
            assert unreconciled.reason_code == "inventory_unreconciled"
        else:
            # A bad profile stays unavailable/unknown even when inventory is unknown,
            # so confirming an unknown inventory can never admit it.
            assert unreconciled.reason_code == reason_code
        assert unreconciled.profile is not None


def test_worker_creation_shares_the_reader_refresh_decision(tmp_path, monkeypatch):
    import src.manager as manager_module

    logged_out = _claude_profile(tmp_path / "claude", {"claudeAiOauth": {}})
    monkeypatch.setattr(manager_module.settings, "HOST_CLAUDE_VALIDATION_PATH", str(logged_out))
    with pytest.raises(RuntimeError, match="refresh-capable"):
        manager_module.WorkerManager._validate_host_session(
            AgentType.CLAUDE, "host_session", "/docker-host/.claude", None
        )

    now = datetime.now(UTC)
    expiring = _codex_profile(
        tmp_path / "codex", _codex_auth(refresh=_jwt(exp=_epoch(now + timedelta(hours=2))))
    )
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_VALIDATION_PATH", str(expiring))
    # Degraded is still admissible until the refresh credential actually expires.
    manager_module.WorkerManager._validate_host_session(
        AgentType.CODEX, "host_session", None, "/docker-host/.codex"
    )

    expired = _codex_profile(
        tmp_path / "codex-expired", _codex_auth(refresh=_jwt(exp=_epoch(now - timedelta(hours=2))))
    )
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_VALIDATION_PATH", str(expired))
    with pytest.raises(RuntimeError, match="refresh credential has expired"):
        manager_module.WorkerManager._validate_host_session(
            AgentType.CODEX, "host_session", None, "/docker-host/.codex"
        )


# --- Codex observation order: stable locked read, then auth_mode, then tokens -------


@contextmanager
def _worker_holds_profile_lock(profile: Path):
    """What `worker_wrapper.wrapper.codex_profile_lock` does for a whole Codex process."""
    lock_path = profile / ".codegen-codex.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        lock_path.chmod(0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _truncate_in_place(auth_path: Path) -> None:
    """The pinned CLI's `FileAuthStorage::save`: open with truncate, same inode and mode."""
    with auth_path.open("r+", encoding="utf-8") as handle:
        handle.truncate(0)


def test_the_reader_joins_the_lock_the_worker_wrapper_takes():
    from src.codex_auth import CODEX_PROFILE_LOCK_NAME

    wrapper = ROOT_DIR / "packages/worker-wrapper/src/worker_wrapper/wrapper.py"
    assert f'CODEX_PROFILE_LOCK_NAME = "{CODEX_PROFILE_LOCK_NAME}"' in wrapper.read_text()


def test_a_torn_read_while_a_cli_holds_the_lock_is_contended_not_logged_out(tmp_path, monkeypatch):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    profile = _codex_profile(tmp_path, _codex_auth())
    auth_path = profile / "auth.json"

    with _worker_holds_profile_lock(profile):
        _truncate_in_place(auth_path)
        empty = inspect_codex_host_session(str(profile), now=NOW)
        auth_path.write_text('{"auth_mode": "chatgpt", "tokens": {"access_')
        partial = inspect_codex_host_session(str(profile), now=NOW)
        # Worker creation does not refuse, or claim, what it could not read.
        validate_codex_host_session(str(profile))

    for inspection in (empty, partial):
        assert inspection.observation.condition is ExecutorProfileCondition.READ_CONTENDED
        assert inspection.observation.login_state is ProfileLoginState.UNKNOWN
        assert inspection.refusal is None


def test_the_same_empty_file_without_a_cli_holding_the_lock_is_logged_out(tmp_path):
    profile = _codex_profile(tmp_path, _codex_auth())
    with _worker_holds_profile_lock(profile):
        pass  # the lock file exists, as after any worker run, but nothing holds it
    _truncate_in_place(profile / "auth.json")

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.LOGGED_OUT
    assert inspection.refusal is not None


def test_an_in_place_refresh_during_the_read_yields_the_new_stable_observation(
    tmp_path, monkeypatch
):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_ATTEMPTS", 200)
    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.01)
    profile = _codex_profile(tmp_path, _codex_auth())
    auth_path = profile / "auth.json"
    refreshed_exp = NOW + timedelta(days=10)
    refreshed = json.dumps(_codex_auth(access=_jwt(exp=_epoch(refreshed_exp))))
    truncated = threading.Event()

    def cli_refresh():
        with auth_path.open("r+", encoding="utf-8") as handle:
            handle.truncate(0)
            handle.flush()
            truncated.set()
            time.sleep(0.15)
            handle.write(refreshed)

    with _worker_holds_profile_lock(profile):
        writer = threading.Thread(target=cli_refresh)
        writer.start()
        assert truncated.wait(2)
        inspection = inspect_codex_host_session(str(profile), now=NOW)
        writer.join()

    assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY
    assert inspection.observation.session_expires_at == refreshed_exp
    assert inspection.refusal is None


def test_diagnostics_publish_a_contended_codex_read_as_unknown(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import src.codex_auth as codex_module
    import src.executor_diagnostics as diagnostics_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", None, raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/docker-host/.codex")
    profile = _codex_profile(tmp_path, _codex_auth())
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    now = datetime.now(UTC)

    with _worker_holds_profile_lock(profile):
        _truncate_in_place(profile / "auth.json")
        diagnostic = diagnostics_module.ExecutorDiagnostics(
            redis=AsyncMock(), docker=MagicMock(), alerts=MagicMock()
        )._executor_diagnostic(
            AgentType.CODEX,
            now,
            now + timedelta(seconds=90),
            {AgentType.CLAUDE: 0, AgentType.CODEX: 1},
        )

    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.reason_code == "profile_read_contended"


def test_the_reader_releases_its_shared_lock(tmp_path):
    profile = _codex_profile(tmp_path, _codex_auth())
    with _worker_holds_profile_lock(profile):
        pass

    inspect_codex_host_session(str(profile), now=NOW)

    with (profile / ".codegen-codex.lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


API_KEY_SECRET = "sk-proj-SYNTHETIC-API-KEY"  # noqa: S105 - synthetic fixture
SUBSCRIPTION_REFUSAL = "Codex auth.json is not in the ChatGPT subscription auth_mode"
FORMAT_REFUSAL = "Codex auth.json does not match the pinned Codex CLI auth format"


@pytest.mark.parametrize(
    ("overrides", "subscription"),
    [
        ({"auth_mode": "chatgpt"}, True),
        # An explicit ChatGPT mode wins over a stored API key, as in the CLI.
        ({"auth_mode": "chatgpt", "OPENAI_API_KEY": API_KEY_SECRET}, True),
        ({"auth_mode": ...}, True),  # absent mode with no competing credential
        ({"auth_mode": "apikey", "OPENAI_API_KEY": API_KEY_SECRET}, False),
        ({"auth_mode": "apikey"}, False),
        ({"auth_mode": ..., "OPENAI_API_KEY": API_KEY_SECRET}, False),
        ({"auth_mode": ..., "personal_access_token": "pat-SYNTHETIC"}, False),
        (
            {"auth_mode": ..., "bedrock_api_key": {"api_key": "SYNTHETIC", "region": "us-east-1"}},
            False,
        ),
        ({"auth_mode": "chatgptAuthTokens"}, False),
        ({"auth_mode": "headers"}, False),
        ({"auth_mode": "agentIdentity"}, False),
        # Not a pinned AuthMode value: the CLI cannot load the file at all.
        ({"auth_mode": "ChatGPT"}, "format"),
        ({"auth_mode": 1}, "format"),
    ],
)
def test_codex_auth_mode_is_checked_before_retained_chatgpt_tokens(
    tmp_path, monkeypatch, overrides, subscription
):
    import src.executor_diagnostics as diagnostics_module
    import src.manager as manager_module

    auth = _codex_auth()
    for key, value in overrides.items():
        if value is ...:
            auth.pop(key, None)
        else:
            auth[key] = value
    profile = _codex_profile(tmp_path, auth)

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    if subscription is True:
        assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY
        assert inspection.refusal is None
        return
    expected = FORMAT_REFUSAL if subscription == "format" else SUBSCRIPTION_REFUSAL
    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert inspection.refusal == expected
    assert API_KEY_SECRET not in inspection.refusal
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        manager_module.WorkerManager._validate_host_session(
            AgentType.CODEX, "host_session", None, "/docker-host/.codex"
        )
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", None, raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/docker-host/.codex")
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    now = datetime.now(UTC)
    diagnostic = diagnostics_module.ExecutorDiagnostics(
        redis=None, docker=None, alerts=object()
    )._executor_diagnostic(
        AgentType.CODEX, now, now + timedelta(seconds=90), {AgentType.CLAUDE: 0, AgentType.CODEX: 0}
    )
    assert diagnostic.availability is ExecutorAvailability.UNAVAILABLE
    assert API_KEY_SECRET not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    "tokens",
    [
        {"id_token": _jwt(), "access_token": "", "refresh_token": ""},
        {"id_token": _jwt(), "access_token": _jwt(exp=_epoch(NOW)), "refresh_token": ""},
    ],
)
def test_a_non_subscription_mode_wins_over_logged_out_or_missing_refresh_tokens(tmp_path, tokens):
    auth = {"auth_mode": "apikey", "OPENAI_API_KEY": API_KEY_SECRET, "tokens": tokens}

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert "auth_mode" in inspection.refusal


def test_joining_the_lock_is_passive_and_never_opens_for_writing(tmp_path, monkeypatch):
    profile = _codex_profile(tmp_path, {**_codex_auth(), "auth_mode": "apikey"})
    with _worker_holds_profile_lock(profile):
        pass
    before = _tree_state(tmp_path)
    _forbid_side_effects(monkeypatch)

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert _tree_state(tmp_path) == before
    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE


# --- a missing lock never proves the read is uncontended ------------------------------


def _writer_after_the_reader_misses_the_lock(monkeypatch, profile: Path, writer):
    """Reviewer schedule: the reader gets ENOENT, then a wrapper creates and takes the lock."""
    import src.codex_auth as codex_module

    real_acquire = codex_module._acquire_shared_lock
    held: list[object] = []

    def acquire_then_writer_starts(lock_path):
        descriptor = real_acquire(lock_path)
        if held:
            # The writer already runs; later reads (worker creation) see its held lock.
            return descriptor
        assert descriptor is None  # the lock file did not exist yet
        lock_file = lock_path.open("a", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        held.append(lock_file)
        writer()
        return descriptor

    monkeypatch.setattr(codex_module, "_acquire_shared_lock", acquire_then_writer_starts)
    return held


@pytest.mark.parametrize(
    "writer_state",
    ["truncated", "partial", "removed"],
)
def test_a_missing_lock_then_a_writer_is_contended_not_logged_out(
    tmp_path, monkeypatch, writer_state
):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    profile = _codex_profile(tmp_path, _codex_auth())
    (profile / ".codegen-codex.lock").unlink()  # a profile logged in before this rule
    auth_path = profile / "auth.json"

    def writer():
        if writer_state == "truncated":
            _truncate_in_place(auth_path)
        elif writer_state == "partial":
            auth_path.write_text('{"auth_mode": "chatgpt", "tokens": {"id_')
        else:
            auth_path.unlink()

    held = _writer_after_the_reader_misses_the_lock(monkeypatch, profile, writer)
    try:
        inspection = inspect_codex_host_session(str(profile), now=NOW)
        validate_codex_host_session(str(profile))  # worker creation does not refuse
    finally:
        for lock_file in held:
            lock_file.close()

    assert held, "the synthetic writer took the lock"
    assert inspection.observation.condition is ExecutorProfileCondition.READ_CONTENDED
    assert inspection.refusal is None


def test_a_missing_lock_with_a_stable_valid_file_still_reads_the_session(tmp_path):
    profile = _codex_profile(tmp_path, _codex_auth())
    (profile / ".codegen-codex.lock").unlink()

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY


def test_a_missing_lock_and_missing_auth_is_never_logged_out(tmp_path, monkeypatch):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    profile = _codex_profile(tmp_path)  # no auth.json
    (profile / ".codegen-codex.lock").unlink()

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.READ_CONTENDED
    # Once the lock inode exists and nothing holds it, the same profile is logged out.
    (profile / ".codegen-codex.lock").touch()
    assert (
        inspect_codex_host_session(str(profile), now=NOW).observation.condition
        is ExecutorProfileCondition.LOGGED_OUT
    )


def test_a_lock_inode_replaced_after_joining_is_not_authoritative(tmp_path, monkeypatch):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    profile = _codex_profile(tmp_path, _codex_auth())
    lock_path = profile / ".codegen-codex.lock"
    real_flock = fcntl.flock

    def flock_then_replace(descriptor, operation):
        real_flock(descriptor, operation)
        if operation == fcntl.LOCK_SH | fcntl.LOCK_NB and lock_path.exists():
            replacement = profile / "lock.new"
            replacement.touch()
            os.replace(replacement, lock_path)  # the joined inode no longer names the lock
            _truncate_in_place(profile / "auth.json")

    monkeypatch.setattr(codex_module.fcntl, "flock", flock_then_replace)

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.READ_CONTENDED


def test_a_missing_lock_then_a_completed_refresh_reads_the_new_session(tmp_path, monkeypatch):
    import src.codex_auth as codex_module

    monkeypatch.setattr(codex_module, "STABLE_READ_PAUSE_SECONDS", 0.001)
    profile = _codex_profile(tmp_path, _codex_auth())
    (profile / ".codegen-codex.lock").unlink()
    refreshed_exp = NOW + timedelta(days=10)
    refreshed = json.dumps(_codex_auth(access=_jwt(exp=_epoch(refreshed_exp))))

    def writer():
        with (profile / "auth.json").open("r+", encoding="utf-8") as handle:
            handle.truncate(0)
            handle.write(refreshed)

    held = _writer_after_the_reader_misses_the_lock(monkeypatch, profile, writer)
    try:
        inspection = inspect_codex_host_session(str(profile), now=NOW)
    finally:
        for lock_file in held:
            lock_file.close()

    assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY
    assert inspection.observation.session_expires_at == refreshed_exp


# --- the file must load as pinned Codex AuthDotJson / TokenData -----------------------


def _id_token_segments(payload: str) -> str:
    return f"{_b64({'alg': 'RS256'})}.{payload}.c2ln"


@pytest.mark.parametrize(
    "mutate",
    [
        # Reviewer reproduction: numeric API key and no id_token, otherwise ChatGPT-shaped.
        lambda auth: (auth.update(OPENAI_API_KEY=12345), auth["tokens"].pop("id_token")),
        lambda auth: auth.update(OPENAI_API_KEY=12345),
        lambda auth: auth["tokens"].pop("id_token"),
        lambda auth: auth["tokens"].update(id_token=None),
        lambda auth: auth["tokens"].update(id_token="opaque-id-token"),  # noqa: S106 - synthetic
        lambda auth: auth["tokens"].update(id_token=_id_token_segments("bm90LWpzb24")),
        lambda auth: auth["tokens"].update(
            id_token=_id_token_segments(_b64({"sub": "x"}) + "==")  # padding is not NO_PAD
        ),
        lambda auth: auth["tokens"].update(id_token=f"{_b64({'alg': 'RS256'})}.{_b64({})}."),
        lambda auth: auth["tokens"].update(
            id_token=_jwt(**{"https://api.openai.com/auth": {"chatgpt_account_is_fedramp": "no"}})
        ),
        lambda auth: auth["tokens"].update(
            id_token=_jwt(**{"https://api.openai.com/auth": {"chatgpt_plan_type": 7}})
        ),
        lambda auth: auth["tokens"].update(id_token=_jwt(email=["x"])),
        lambda auth: auth["tokens"].update(access_token=7),
        lambda auth: auth["tokens"].update(account_id=42),
        lambda auth: auth.update(tokens=["id", "access", "refresh"]),
        lambda auth: auth.update(personal_access_token=1),
        lambda auth: auth.update(agent_identity=1),
        lambda auth: auth.update(bedrock_api_key={"api_key": "SYNTHETIC"}),
        lambda auth: auth.update(bedrock_api_key="SYNTHETIC"),
        lambda auth: auth.update(auth_mode="chatgpt_tokens"),
        lambda auth: auth.update(last_refresh="2026-09-13T08:30:00"),  # naive
        lambda auth: auth.update(last_refresh="yesterday"),
        lambda auth: auth.update(last_refresh=1757750000),
    ],
)
def test_a_file_the_pinned_cli_cannot_load_is_refused_by_admission_and_diagnostics(
    tmp_path, monkeypatch, mutate
):
    import src.executor_diagnostics as diagnostics_module
    import src.manager as manager_module

    auth = _codex_auth()
    mutate(auth)
    profile = _codex_profile(tmp_path, auth)

    inspection = inspect_codex_host_session(str(profile), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.UNUSABLE
    assert inspection.refusal == FORMAT_REFUSAL
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    with pytest.raises(RuntimeError, match=re.escape(FORMAT_REFUSAL)):
        manager_module.WorkerManager._validate_host_session(
            AgentType.CODEX, "host_session", None, "/docker-host/.codex"
        )
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", None, raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/docker-host/.codex")
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", str(profile))
    now = datetime.now(UTC)
    diagnostic = diagnostics_module.ExecutorDiagnostics(
        redis=None, docker=None, alerts=object()
    )._executor_diagnostic(
        AgentType.CODEX, now, now + timedelta(seconds=90), {AgentType.CLAUDE: 0, AgentType.CODEX: 0}
    )
    assert (diagnostic.availability, diagnostic.reason_code) == (
        ExecutorAvailability.UNAVAILABLE,
        "local_auth_invalid",
    )
    serialized = diagnostic.model_dump_json()
    assert OPAQUE_CODEX_REFRESH not in serialized and "eyJ" not in serialized


@pytest.mark.parametrize(
    "mutate",
    [
        # Fields and claims the pinned CLI ignores, and nulls its Option fields accept.
        lambda auth: auth.update(future_field={"nested": [1, 2]}),
        lambda auth: auth["tokens"].update(future_token_field=3),
        lambda auth: auth.update(OPENAI_API_KEY=None, personal_access_token=None),
        lambda auth: auth.update(agent_identity=None, bedrock_api_key=None, auth_mode=None),
        lambda auth: auth["tokens"].update(account_id=None),
        lambda auth: auth.update(last_refresh=None),
        lambda auth: auth.update(last_refresh="2026-09-13T10:30:00+02:00"),
        lambda auth: auth["tokens"].update(
            id_token=_jwt(
                email=None,
                unknown_claim={"x": 1},
                **{
                    "https://api.openai.com/profile": {"email": "person@example.com"},
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "mystery-tier",
                        "chatgpt_account_id": "acct",
                        "chatgpt_account_is_fedramp": False,
                    },
                },
            )
        ),
        lambda auth: auth["tokens"].update(id_token=_jwt() + ".extra-segment"),
    ],
)
def test_fields_the_pinned_cli_ignores_do_not_make_a_session_unusable(tmp_path, mutate):
    auth = _codex_auth()
    mutate(auth)

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.HEALTHY
    assert inspection.refusal is None
