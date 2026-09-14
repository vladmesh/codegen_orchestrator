"""Passive, credential-safe host-session profile readers.

Every profile here is synthetic. No test authenticates a CLI or reads a real
profile; the passivity tests make any subprocess, copy, network or Docker call
fail loudly.
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

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
from src.codex_auth import inspect_codex_host_session
from src.host_profile import jwt_expiry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
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
        ({"tokens": {}}, None),
        ({"tokens": {"access_token": "", "refresh_token": ""}}, None),
    ],
)
def test_codex_logged_out_profiles(tmp_path, auth, raw):
    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth, raw=raw)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.LOGGED_OUT
    assert inspection.refusal is not None


def test_codex_missing_refresh_token_is_unavailable(tmp_path):
    auth = _codex_auth(refresh=None)

    inspection = inspect_codex_host_session(str(_codex_profile(tmp_path, auth)), now=NOW)

    assert inspection.observation.condition is ExecutorProfileCondition.REFRESH_MISSING
    assert "refresh-capable" in inspection.refusal


@pytest.mark.parametrize(
    ("auth", "raw", "config"),
    [
        (None, "{not json", None),
        ([], None, None),
        ({"tokens": "token"}, None, None),
        ({"tokens": {"access_token": 1, "refresh_token": "x"}}, None, None),
        ({"tokens": {"refresh_token": OPAQUE_CODEX_REFRESH}}, None, None),
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
        {"last_refresh": "2026-09-13T08:30:00"},  # naive
        {"last_refresh": "yesterday"},
        {"last_refresh": 1757750000},
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
        source = (root / name).read_text()
        for forbidden in (
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
        "logged_out": ({"tokens": {}}, ExecutorAvailability.UNAVAILABLE, "profile_logged_out"),
        "refresh_missing": (
            _codex_auth(refresh=None),
            ExecutorAvailability.UNAVAILABLE,
            "profile_refresh_missing",
        ),
        "unverifiable": (
            _codex_auth(last_refresh="bad"),
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
