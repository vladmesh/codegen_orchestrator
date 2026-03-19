"""Tests for agent subprocess environment — PYTHONPATH must not shadow project packages."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


def _make_config(**overrides) -> WorkerWrapperConfig:
    defaults = {
        "redis_url": "redis://localhost:6379",
        "input_stream": "worker:test:input",
        "output_stream": "worker:test:output",
        "consumer_group": "test_group",
        "consumer_name": "test_worker",
        "agent_type": "noop",
    }
    defaults.update(overrides)
    return WorkerWrapperConfig(**defaults)


def _make_wrapper() -> WorkerWrapper:
    """Create a WorkerWrapper with mocked Redis."""
    mock_redis = MagicMock()
    mock_redis.redis = AsyncMock()
    # SessionManager.get_or_create_session needs hget
    mock_redis.redis.hget = AsyncMock(return_value=None)
    mock_redis.redis.hset = AsyncMock()
    wrapper = WorkerWrapper(config=_make_config(), redis_client=mock_redis)
    return wrapper


def _fake_subprocess():
    """Return a fake create_subprocess_exec and a dict to capture kwargs."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        proc.kill = AsyncMock()
        proc.wait = AsyncMock()
        return proc

    return fake_exec, captured


class TestAgentSubprocessEnv:
    """execute_agent must strip /app from PYTHONPATH for the agent subprocess.

    The worker image sets PYTHONPATH=/app so the wrapper can import the
    orchestrator's ``shared``.  But the agent runs in a scaffolded project
    whose venvs have their own ``shared`` (via .pth editable links).
    Keeping /app shadows the project's shared and causes ModuleNotFoundError.
    """

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
    async def test_subprocess_env_inherits_other_vars(self):
        """Subprocess env must preserve non-PYTHONPATH vars (PATH, HOME, etc)."""
        wrapper = _make_wrapper()
        fake_exec, captured = _fake_subprocess()

        fake_env = {"PATH": "/usr/bin", "HOME": "/home/worker", "PYTHONPATH": "/app"}

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.object(wrapper, "_resolve_prompt", return_value="do stuff"),
            patch.dict("os.environ", fake_env, clear=True),
        ):
            await wrapper.execute_agent({"prompt": "test"})

        env = captured.get("env", {})
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/home/worker"

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
