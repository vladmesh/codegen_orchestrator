"""Unit tests for the dev-env CLI commands."""

import sys
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

# Ensure redis.asyncio is importable in the test environment (mirrors test_respond.py)
if "redis.asyncio" not in sys.modules:
    _mock_redis = MagicMock()
    sys.modules.setdefault("redis", _mock_redis)
    sys.modules.setdefault("redis.asyncio", _mock_redis)

from orchestrator_cli.commands.dev_env import app


@pytest.fixture(autouse=True)
def worker_env(monkeypatch):
    """Set required environment variables for CLI tests."""
    monkeypatch.setenv("WORKER_ID", "worker-test-123")
    monkeypatch.setenv("ORCHESTRATOR_API_URL", "http://api:8000")
    monkeypatch.setenv("ORCHESTRATOR_REDIS_URL", "redis://redis:6379")
    monkeypatch.setenv("ORCHESTRATOR_WORKER_MANAGER_URL", "http://worker-manager:8000")


runner = CliRunner()


def _mock_compose_response(exit_code: int = 0, stdout: str = "", stderr: str = ""):
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def test_patch_db_hostname_removed():
    """_patch_db_hostname workaround removed — workers on codegen_worker (#22)."""
    import orchestrator_cli.commands.dev_env as mod

    assert not hasattr(
        mod, "_patch_db_hostname"
    ), "_patch_db_hostname should be removed: workers on codegen_worker"


class TestDevEnvCompose:
    def test_start_infra_calls_up_with_wait(self):
        """start-infra should issue 'up -d --wait' to the compose endpoint."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            captured["args"] = args
            captured["worker_id"] = worker_id
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["start-infra", "db", "redis"])

        assert result.exit_code == 0
        assert captured["args"] == ["up", "-d", "--wait", "db", "redis"]
        assert captured["worker_id"] == "worker-test-123"

    def test_start_infra_with_file_option(self):
        """start-infra -f should prepend file flags."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            captured["args"] = args
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["start-infra", "-f", "infra/compose.base.yml", "db"])

        assert result.exit_code == 0
        assert captured["args"] == ["-f", "infra/compose.base.yml", "up", "-d", "--wait", "db"]

    def test_stop_infra_calls_stop(self):
        """stop-infra should issue 'stop' to the compose endpoint."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=60):
            captured["args"] = args
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["stop-infra"])

        assert result.exit_code == 0
        assert captured["args"] == ["stop"]

    def test_reset_infra_calls_down_v(self):
        """reset-infra should issue 'down -v' to the compose endpoint."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            captured["args"] = args
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["reset-infra"])

        assert result.exit_code == 0
        assert captured["args"] == ["down", "-v"]

    def test_reset_infra_with_file_option(self):
        """reset-infra -f should prepend file flags."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            captured["args"] = args
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["reset-infra", "-f", "infra/compose.base.yml"])

        assert result.exit_code == 0
        assert captured["args"] == ["-f", "infra/compose.base.yml", "down", "-v"]

    def test_compose_passes_worker_id_from_env(self):
        """compose should use WORKER_ID env var."""
        captured = {}

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            captured["worker_id"] = worker_id
            return _mock_compose_response()

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            runner.invoke(app, ["start-infra"])

        assert captured["worker_id"] == "worker-test-123"

    def test_compose_nonzero_exit_propagates(self):
        """A non-zero compose exit code should exit with that code."""
        expected_exit_code = 2

        async def fake_compose(worker_id, args, cwd=".", timeout=120):
            return _mock_compose_response(exit_code=expected_exit_code, stderr="compose error\n")

        with patch("orchestrator_cli.commands.dev_env._compose_async", new=fake_compose):
            result = runner.invoke(app, ["start-infra"])

        assert result.exit_code == expected_exit_code
