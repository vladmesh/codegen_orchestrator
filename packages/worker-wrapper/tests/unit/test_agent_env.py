"""Tests for the allowlisted environment passed to agent subprocesses."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


def _make_config(**overrides) -> WorkerWrapperConfig:
    defaults = {
        "broker_url": "http://worker-broker:8001",
        "broker_token": "x" * 43,
        "worker_id": "test_worker",
        "agent_type": "noop",
    }
    defaults.update(overrides)
    return WorkerWrapperConfig(**defaults)


def _make_wrapper(**config_overrides) -> WorkerWrapper:
    """Create a WorkerWrapper with a mocked broker client."""
    mock_redis = MagicMock()
    mock_redis.redis = AsyncMock()
    mock_redis.get_session = AsyncMock(return_value=None)
    mock_redis.set_session = AsyncMock()
    wrapper = WorkerWrapper(config=_make_config(**config_overrides), broker_client=mock_redis)
    return wrapper


def _fake_subprocess(stdout: bytes = b"", stderr: bytes = b""):
    """Return a fake create_subprocess_exec and a dict to capture kwargs."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = 0
        proc.kill = AsyncMock()
        proc.wait = AsyncMock()
        return proc

    return fake_exec, captured


class TestAgentSubprocessEnv:
    """Agent subprocesses receive only their declared runtime environment."""

    _ALLOWED_AGENT_SETTINGS = {
        "ANTHROPIC_API_KEY": "ant-key",
        "ANTHROPIC_AUTH_TOKEN": "ant-token",
        "ANTHROPIC_BASE_URL": "https://anthropic.example",
        "CLAUDE_CONFIG_DIR": "/home/worker/.claude",
        "CODEX_API_KEY": "codex-key",
        "CODEX_HOME": "/home/worker/.codex",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "FACTORY_API_KEY": "factory-key",
        "GITHUB_TOKEN": "github-token",
        "GH_TOKEN": "github-token",
        "PYTHONNOUSERSITE": "1",
    }

    _BLOCKED_WRAPPER_SETTINGS = {
        "WORKER_MANAGER_URL": "http://worker-manager:8000",
        "WORKER_API_URL": "http://api:8000",
        "WORKER_REDIS_URL": "redis://redis:6379",
        "SECRETS_ENCRYPTION_KEY": "wrapper-secret",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONFIG": "/root/.docker",
        "DOCKER_CERT_PATH": "/root/.docker/certs",
        "COMPOSE_FILE": "/app/compose.yml",
        "HOST_CLAUDE_DIR": "/host/claude",
        "HOST_CODEX_HOME": "/host/codex",
        "HOST_WORKSPACE_PATH": "/host/workspace",
        "COMMAND_INJECTED_VAR": "must-not-reach-agent",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }

    @classmethod
    def _wrapper_environment(cls) -> dict[str, str]:
        return {
            "PATH": "/usr/bin",
            "HOME": "/home/worker",
            "LANG": "C.UTF-8",
            "PYTHONPATH": "/app:/extra/lib:/more",
            **cls._ALLOWED_AGENT_SETTINGS,
            **cls._BLOCKED_WRAPPER_SETTINGS,
        }

    @pytest.mark.asyncio
    async def test_subprocess_strips_app_from_pythonpath(self):
        """PYTHONPATH=/app must be removed for the agent subprocess."""
        wrapper = _make_wrapper()
        fake_exec, captured = _fake_subprocess()

        fake_env = {"PATH": "/usr/bin", "PYTHONPATH": "/app"}

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", fake_env, clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        assert "env" in captured, "execute_agent must pass env= to create_subprocess_exec"
        env = captured["env"]

        # /app must NOT be in PYTHONPATH
        pythonpath = env.get("PYTHONPATH", "")
        parts = [p for p in pythonpath.split(os.pathsep) if p]
        assert "/app" not in parts, f"PYTHONPATH must not contain /app, got: {pythonpath}"

    @pytest.mark.asyncio
    async def test_subprocess_preserves_non_app_pythonpath(self):
        """Other PYTHONPATH entries (non /app) must be preserved."""
        wrapper = _make_wrapper()
        fake_exec, captured = _fake_subprocess()

        fake_env = {"PYTHONPATH": "/app:/extra/lib:/more"}

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", fake_env, clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        env = captured["env"]
        pythonpath = env.get("PYTHONPATH", "")
        parts = [p for p in pythonpath.split(os.pathsep) if p]
        assert "/app" not in parts
        assert "/extra/lib" in parts
        assert "/more" in parts

    @pytest.mark.asyncio
    async def test_subprocess_uses_allowlist_without_wrapper_control_plane_settings(self):
        """Normal execution keeps the CLI settings and drops wrapper-only values."""
        wrapper = _make_wrapper()
        fake_exec, captured = _fake_subprocess()

        fake_env = self._wrapper_environment()

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", fake_env, clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        env = captured.get("env", {})
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/home/worker"
        assert env.get("LANG") == "C.UTF-8"
        assert env.get("PYTHONPATH") == "/extra/lib:/more"
        for name, value in self._ALLOWED_AGENT_SETTINGS.items():
            assert env.get(name) == value
        for name in self._BLOCKED_WRAPPER_SETTINGS:
            assert name not in env

    @pytest.mark.asyncio
    async def test_transcript_redacts_non_allowlisted_wrapper_secret(self, tmp_path):
        """Transcript redaction must retain wrapper-only secrets as scrub sources."""
        wrapper = _make_wrapper(transcript_dir=str(tmp_path))
        fake_exec, captured = _fake_subprocess(stdout=b"wrapper-secret")

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", self._wrapper_environment(), clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test", "request_id": "redaction"})

        assert "SECRETS_ENCRYPTION_KEY" not in captured["env"]
        transcript = (tmp_path / "test_worker" / "redaction.log").read_text()
        assert "wrapper-secret" not in transcript
        assert "[redacted]" in transcript

    @pytest.mark.asyncio
    async def test_auto_resume_uses_the_same_allowlist(self):
        """Claude auto-resume must not regain wrapper credentials or sockets."""
        wrapper = _make_wrapper(agent_type="claude")
        wrapper.broker.get_session.return_value = "session-for-resume"
        fake_exec, captured = _fake_subprocess()

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.dict("os.environ", self._wrapper_environment(), clear=True),
        ):
            resumed = await wrapper._attempt_auto_resume({"prompt": "test"})

        assert resumed is True
        env = captured["env"]
        assert env.get("PYTHONPATH") == "/extra/lib:/more"
        for name, value in self._ALLOWED_AGENT_SETTINGS.items():
            assert env.get(name) == value
        for name in self._BLOCKED_WRAPPER_SETTINGS:
            assert name not in env

    @pytest.mark.asyncio
    async def test_subprocess_removes_pythonpath_when_only_app(self):
        """If PYTHONPATH is only /app, it should be removed entirely."""
        wrapper = _make_wrapper()
        fake_exec, captured = _fake_subprocess()

        fake_env = {"PATH": "/usr/bin", "PYTHONPATH": "/app"}

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", fake_env, clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        env = captured["env"]
        assert "PYTHONPATH" not in env or env["PYTHONPATH"] == ""

    @pytest.mark.asyncio
    async def test_failure_error_uses_stdout_when_stderr_empty(self):
        """Some CLIs emit structured failures on stdout, not stderr."""
        wrapper = _make_wrapper()

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b'{"result":"auth failed"}', b""))
            proc.returncode = 1
            proc.kill = AsyncMock()
            proc.wait = AsyncMock()
            return proc

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
        ):
            with pytest.raises(RuntimeError, match="auth failed"):
                await wrapper.execute_agent({"prompt": "test"})

    @pytest.mark.asyncio
    async def test_codex_stand_token_login_uses_stdin_and_does_not_reach_subprocess_env_or_argv(
        self, tmp_path
    ):
        wrapper = _make_wrapper(agent_type="codex", auth_mode="stand_token", worker_type="qa")
        calls: list[tuple[tuple, dict, AsyncMock]] = []

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            calls.append((args, kwargs, proc))
            return proc

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict(
                "os.environ",
                {
                    "PATH": "/usr/bin",
                    "HOME": "/home/worker",
                    "CODEX_HOME": str(tmp_path / ".codex"),
                    "CODEX_ACCESS_TOKEN": "fake-codex-token",
                    "HTTPS_PROXY": "http://qa-proxy:3128",
                    "https_proxy": "http://qa-proxy:3128",
                    "NO_PROXY": "localhost,127.0.0.1",
                    "no_proxy": "localhost,127.0.0.1",
                },
                clear=True,
            ),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        login_args, login_kwargs, login_proc = calls[0]
        agent_args, agent_kwargs, _agent_proc = calls[1]
        assert login_args == ("codex", "login", "--with-access-token")
        assert "fake-codex-token" not in login_args
        assert "CODEX_ACCESS_TOKEN" not in login_kwargs["env"]
        login_proc.communicate.assert_awaited_once_with(b"fake-codex-token")
        assert login_kwargs["env"] == {
            "HOME": "/home/worker",
            "PATH": "/usr/bin",
            "CODEX_HOME": str(tmp_path / ".codex"),
            "HTTPS_PROXY": "http://qa-proxy:3128",
            "https_proxy": "http://qa-proxy:3128",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
        assert "fake-codex-token" not in agent_args
        assert "CODEX_ACCESS_TOKEN" not in agent_kwargs["env"]
        assert (
            tmp_path / ".codex" / "config.toml"
        ).read_text() == 'cli_auth_credentials_store = "file"\n'
        assert not (tmp_path / ".codex" / "auth.json").exists()
