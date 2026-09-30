"""The CLI channels inside the langgraph and architect containers.

Those containers run as root and share the Codex workers' own profile, so a
`codex` child runs as the profile's owner and a `claude` child as `nobody`,
each with a HOME of its own; a profile the channel cannot use safely is a
`missing_credential` channel failure that names what is wrong. At startup each
agent logs `llm_channel_ready` per channel. The fake CLIs of `fake_cli.py`
stand for the real ones; running as root is simulated by `geteuid`, with the
uid switch recorded instead of performed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import pwd
import re

import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig
from src.llm import LLMAgent, build_agent_llm, log_channel_readiness
from src.llm.cli_turn import CODEX_PROFILE_LOCK_NAME
from tests.unit.llm.conftest import OPENROUTER_KEY
from tests.unit.llm.test_channel_chain import DEFAULT, _ask, _chain

REPO = Path(__file__).resolve().parents[5]
OWNER_UID, OWNER_GID = 4242, 4343


@pytest.fixture
def as_root(monkeypatch, channels):
    """This process looks like root; the profile looks owned by `OWNER_UID:OWNER_GID`.

    Records every chown and every subprocess's identity switch, and performs
    neither: the test process cannot become another user.
    """
    real_stat = os.stat
    owned = {channels.codex_home, channels.codex_home / "auth.json"}
    chowns: list[tuple[str, int, int]] = []
    switches: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    def fake_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - os.stat's shape
        result = real_stat(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and Path(path) in owned:
            values = list(result[:10])
            values[4], values[5] = OWNER_UID, OWNER_GID
            return os.stat_result(values)
        return result

    async def recording_exec(*command, **kwargs):  # noqa: ANN002, ANN003
        switches.append(
            {key: kwargs.pop(key) for key in ("user", "group", "extra_groups") if key in kwargs}
        )
        return await real_exec(*command, **kwargs)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowns.append((str(path), uid, gid)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    return chowns, switches


# --- who the child runs as ----------------------------------------------------


class TestChildUser:
    async def test_codex_runs_as_the_profile_owner_with_no_supplementary_groups(
        self, channels, as_root
    ):
        chowns, switches = as_root
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain("codex"), channels.settings())

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "codex"
        assert switches == [{"user": OWNER_UID, "group": OWNER_GID, "extra_groups": []}]
        [call] = channels.codex.calls
        home = Path(call["env"]["HOME"])
        handed_over = {
            Path(path) for path, uid, gid in chowns if (uid, gid) == (OWNER_UID, OWNER_GID)
        }
        # The call's root, workdir, io dir (and the schema in it) and HOME become the child's.
        assert {home, home.parent, Path(call["cwd"])} <= handed_over
        assert any(path.name == "output-schema.json" for path in handed_over)

    async def test_the_lock_a_root_process_creates_is_the_profile_owners_before_it_exists(
        self, channels, as_root
    ):
        """The wrapper chmods the lock before each turn; a root-owned lock would fail it."""
        chowns, _ = as_root
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain("codex"), channels.settings())

        await _ask(llm)

        lock = channels.codex_home / CODEX_PROFILE_LOCK_NAME
        assert lock.is_file() and (lock.stat().st_mode & 0o777) == 0o600  # noqa: PLR2004
        [staged] = [
            Path(path)
            for path, uid, gid in chowns
            if Path(path).name.startswith(f"{CODEX_PROFILE_LOCK_NAME}.")
        ]
        assert staged.parent == channels.codex_home
        assert not staged.exists(), "the staging name is linked into place, then removed"
        # Nothing but the lock was added to the profile: it is used in place, never copied.
        assert sorted(p.name for p in channels.codex_home.iterdir()) == [
            CODEX_PROFILE_LOCK_NAME,
            "auth.json",
            "config.toml",
        ]

    async def test_claude_runs_as_nobody_when_this_process_is_root(self, channels, as_root):
        _, switches = as_root
        nobody = pwd.getpwnam("nobody")
        llm = build_agent_llm(LLMAgent.PO, _chain("claude"), channels.settings())

        await _ask(llm)

        assert switches == [{"user": nobody.pw_uid, "group": nobody.pw_gid, "extra_groups": []}]

    async def test_a_non_root_process_runs_the_child_as_itself(self, channels, monkeypatch):
        switches = []
        real_exec = asyncio.create_subprocess_exec

        async def recording_exec(*command, **kwargs):  # noqa: ANN002, ANN003
            switches.append({key for key in ("user", "group", "extra_groups") if key in kwargs})
            return await real_exec(*command, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
        llm = build_agent_llm(LLMAgent.PO, _chain("codex", "claude"), channels.settings())

        await _ask(llm)

        assert switches == [set()]

    async def test_each_call_gets_a_fresh_home_that_is_gone_afterwards(self, channels):
        llm = build_agent_llm(LLMAgent.PO, _chain("codex"), channels.settings())

        await _ask(llm)
        await _ask(llm)

        homes = [call["env"]["HOME"] for call in channels.codex.calls]
        assert len(set(homes)) == 2  # noqa: PLR2004
        assert all(call["home_writable"] for call in channels.codex.calls)
        assert not any(Path(home).exists() for home in homes)
        assert all(channels.codex_home not in Path(home).parents for home in homes)


# --- an unsuitable profile ------------------------------------------------------


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text)
    path.chmod(mode)


UNSUITABLE = {
    "directory-mode": (lambda home: home.chmod(0o755), "must have mode 0700"),
    "auth-absent": (lambda home: (home / "auth.json").unlink(), "no non-empty auth.json"),
    "auth-empty": (lambda home: _write(home / "auth.json", ""), "no non-empty auth.json"),
    "auth-mode": (lambda home: (home / "auth.json").chmod(0o644), "auth.json must have mode 0600"),
    "config-absent": (lambda home: (home / "config.toml").unlink(), "has no config.toml"),
    "config-mode": (
        lambda home: (home / "config.toml").chmod(0o644),
        "config.toml must have mode 0600",
    ),
    "keyring-store": (
        lambda home: _write(home / "config.toml", 'cli_auth_credentials_store = "keyring"\n'),
        'cli_auth_credentials_store = "file"',
    ),
    "config-invalid": (
        lambda home: _write(home / "config.toml", "cli_auth_credentials_store = \n"),
        "unreadable or invalid TOML",
    ),
}


class TestUnsuitableProfile:
    async def _codex_failure(self, channels, settings=None) -> dict:
        llm = build_agent_llm(
            LLMAgent.ARCHITECT, _chain("codex", "claude"), settings or channels.settings()
        )
        with capture_logs() as logs:
            answer = await _ask(llm)
        assert answer.response_metadata["llm_channel"] == "claude"
        assert not channels.codex.calls, "codex ran on an unsuitable profile"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert (failed["channel"], failed["failure_class"]) == ("codex", "missing_credential")
        return failed

    @pytest.mark.parametrize("problem", sorted(UNSUITABLE))
    async def test_is_a_named_missing_credential_and_the_chain_moves_on(self, channels, problem):
        spoil, reason = UNSUITABLE[problem]
        spoil(channels.codex_home)

        failed = await self._codex_failure(channels)

        assert reason in failed["reason"]
        assert not (channels.codex_home / CODEX_PROFILE_LOCK_NAME).exists()

    async def test_a_profile_path_that_is_not_a_directory(self, channels, tmp_path):
        not_a_directory = tmp_path / "file"
        not_a_directory.write_text("x")

        failed = await self._codex_failure(
            channels, channels.settings(llm_codex_home=str(not_a_directory))
        )

        assert "not an existing directory" in failed["reason"]

    async def test_a_root_owned_profile_is_refused(self, channels, monkeypatch):
        real_stat = os.stat
        home = channels.codex_home

        def root_owned(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            result = real_stat(path, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)) and Path(path) == home:
                values = list(result[:10])
                values[4] = values[5] = 0
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "stat", root_owned)

        failed = await self._codex_failure(channels)

        assert "owned by root" in failed["reason"]

    async def test_auth_owned_by_someone_else_is_refused(self, channels, as_root, monkeypatch):
        real_stat = os.stat  # already the as_root fake: the directory is OWNER_UID's
        auth = channels.codex_home / "auth.json"

        def foreign_auth(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            result = real_stat(path, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)) and Path(path) == auth:
                values = list(result[:10])
                values[4] = OWNER_UID + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "stat", foreign_auth)

        failed = await self._codex_failure(channels)

        assert "not owned by the profile's owner" in failed["reason"]

    async def test_a_process_that_can_neither_be_nor_become_the_owner(self, channels, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

        failed = await self._codex_failure(channels)

        assert "cannot run the CLI as that uid" in failed["reason"]


# --- the lock the three consumers share ----------------------------------------


def _constant(path: Path, name: str) -> str:
    match = re.search(rf'^{name} = "([^"]+)"$', path.read_text(), flags=re.MULTILINE)
    assert match is not None, f"{name} is not defined in {path}"
    return match.group(1)


def test_the_profile_lock_is_the_one_the_worker_wrapper_and_worker_manager_use():
    wrapper = REPO / "packages/worker-wrapper/src/worker_wrapper/wrapper.py"
    reader = REPO / "services/worker-manager/src/codex_auth.py"

    assert _constant(wrapper, "CODEX_PROFILE_LOCK_NAME") == CODEX_PROFILE_LOCK_NAME
    assert _constant(reader, "CODEX_PROFILE_LOCK_NAME") == CODEX_PROFILE_LOCK_NAME
    assert "profile / CODEX_PROFILE_LOCK_NAME" in wrapper.read_text()


# --- PO timeouts ----------------------------------------------------------------


class TestPoTimeouts:
    @pytest.mark.parametrize("agent", [LLMAgent.PO, LLMAgent.PO_SUMMARIZER])
    def test_the_po_chains_give_a_subscription_cli_three_minutes(self, channels, agent):
        llm = build_agent_llm(agent, _chain(*DEFAULT), channels.settings())

        assert [(slot.channel.value, slot.timeout_seconds) for slot in llm.slots] == [
            ("codex", 180.0),
            ("claude", 180.0),
            ("openrouter", 600.0),
        ]

    def test_the_architect_keeps_the_planning_timeout(self, channels):
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        assert {slot.timeout_seconds for slot in llm.slots} == {600.0}

    def test_a_configured_timeout_wins(self, channels):
        chain = [LLMChannelConfig(channel=LLMChannel.CODEX, timeout_seconds=45)]

        [slot] = build_agent_llm(LLMAgent.PO, chain, channels.settings()).slots

        assert slot.timeout_seconds == 45  # noqa: PLR2004


# --- readiness --------------------------------------------------------------------


class TestReadinessLog:
    async def test_every_channel_of_a_ready_chain_logs_ready_with_its_version(self, channels):
        channels.codex.script(version="codex-cli 0.144.6")
        channels.claude.script(version="2.1.278 (Claude Code)")
        llm = build_agent_llm(LLMAgent.PO, _chain(*DEFAULT), channels.settings())

        with capture_logs() as logs:
            await log_channel_readiness(llm)

        ready = [log for log in logs if log["event"] == "llm_channel_ready"]
        assert [
            (log["agent"], log["channel"], log["status"], log["cli_version"], log["timeout_s"])
            for log in ready
        ] == [
            ("po", "codex", "ready", "codex-cli 0.144.6", 180.0),
            ("po", "claude", "ready", "2.1.278 (Claude Code)", 180.0),
            ("po", "openrouter", "ready", None, 600.0),
        ]
        assert all(log["reason"] is None for log in ready)
        assert "claude-oauth-test-token" not in json.dumps(ready, default=str)
        assert OPENROUTER_KEY not in json.dumps(ready, default=str)

    async def test_the_probe_calls_no_model_and_never_shows_a_cli_its_credential(self, channels):
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        await log_channel_readiness(llm)

        assert not channels.codex.calls and not channels.claude.calls
        assert not channels.openrouter.seen
        for fake in (channels.codex, channels.claude):
            [probe] = fake.version_calls
            assert set(probe["env"]) - {"PWD", "LC_CTYPE"} == {"PATH", "HOME"}
            assert Path(probe["env"]["HOME"]) != channels.codex_home
        assert not (channels.codex_home / CODEX_PROFILE_LOCK_NAME).exists()

    async def test_a_channel_that_would_fail_logs_its_failure_class_and_reason(self, channels):
        (channels.codex_home / "config.toml").unlink()
        (channels.bin_dir / "claude").unlink()
        settings = channels.settings(po_llm_api_key=None)
        llm = build_agent_llm(LLMAgent.PO, _chain(*DEFAULT), settings)

        with capture_logs() as logs:
            await log_channel_readiness(llm)

        ready = {log["channel"]: log for log in logs if log["event"] == "llm_channel_ready"}
        assert ready["codex"]["status"] == "missing_credential"
        assert "config.toml" in ready["codex"]["reason"]
        assert ready["codex"]["cli_version"] == "fake-cli 0.0.0"
        assert (ready["claude"]["status"], ready["claude"]["cli_version"]) == (
            "binary_missing",
            None,
        )
        assert ready["openrouter"]["status"] == "missing_credential"
        assert "PO_LLM_API_KEY" in ready["openrouter"]["reason"]
        assert all(log["log_level"] == "warning" for log in ready.values())

    async def test_a_missing_token_is_reported_with_the_installed_version(self, channels):
        llm = build_agent_llm(
            LLMAgent.PO, _chain("claude"), channels.settings(claude_code_oauth_token=None)
        )

        [readiness] = await log_channel_readiness(llm)

        assert readiness.status == "missing_credential"
        assert readiness.reason == "CLAUDE_CODE_OAUTH_TOKEN is not set"
        assert readiness.cli_version == "fake-cli 0.0.0"


class TestReadinessAtStartup:
    async def test_the_po_and_its_summarizer_log_their_chains(self, channels):
        from unittest.mock import patch

        from src import main
        from tests.unit.llm.test_agent_wiring import _Configs

        with (
            patch.object(main, "api_client", _Configs()),
            patch.object(main, "get_settings", return_value=channels.settings()),
            capture_logs() as logs,
        ):
            await main._po_missing_env()

        ready = [
            (log["agent"], log["channel"], log["status"])
            for log in logs
            if log["event"] == "llm_channel_ready"
        ]
        assert ready == [
            (agent, channel, "ready") for agent in ("po", "po_summarizer") for channel in DEFAULT
        ]

    def test_the_architect_logs_its_chain_before_it_starts(self, channels):
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.consumers import architect

        with (
            patch.object(architect, "api_client", MagicMock(close=AsyncMock())),
            patch.object(architect, "load_channel_chain", AsyncMock(return_value=_chain(*DEFAULT))),
            patch.object(architect, "get_settings", return_value=channels.settings()),
            patch.object(architect, "start_worker") as start_worker,
            capture_logs() as logs,
        ):
            architect.main()

        start_worker.assert_called_once()
        assert [
            (log["agent"], log["channel"], log["status"])
            for log in logs
            if log["event"] == "llm_channel_ready"
        ] == [("architect", channel, "ready") for channel in DEFAULT]
