"""Tests for shared infra_client."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.clients.infra_client import (
    SSH_KEY_PATH,
    SSH_TIMEOUT,
    check_http_health,
    get_container_logs,
    get_container_stats,
    get_container_status,
    run_ssh_command,
)


class TestModuleConstants:
    def test_ssh_key_path_from_shared_constants(self):
        assert SSH_KEY_PATH == "/root/.ssh/id_ed25519"

    def test_ssh_timeout_from_shared_constants(self):
        assert SSH_TIMEOUT == 30


class TestRunSshCommand:
    @pytest.mark.asyncio
    async def test_successful_command(self):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            success, stdout, stderr = await run_ssh_command("1.2.3.4", "echo hello")

        assert success is True
        assert stdout == "output"
        assert stderr == ""

    @pytest.mark.asyncio
    async def test_failed_command(self):
        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            success, stdout, stderr = await run_ssh_command("1.2.3.4", "bad cmd")

        assert success is False
        assert stderr == "error"

    @pytest.mark.asyncio
    async def test_timeout(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=TimeoutError,
        ):
            success, stdout, stderr = await run_ssh_command("1.2.3.4", "slow", timeout=1)

        assert success is False
        assert stderr == "Command timed out"


class TestGetContainerLogs:
    @pytest.mark.asyncio
    async def test_successful_logs(self):
        with patch(
            "shared.clients.infra_client.run_ssh_command",
            return_value=(True, "line1\nline2", ""),
        ):
            result = await get_container_logs("1.2.3.4", "myapp")

        assert result["success"] is True
        assert result["logs"] == "line1\nline2"
        assert result["lines_returned"] == 2

    @pytest.mark.asyncio
    async def test_failed_logs(self):
        with patch(
            "shared.clients.infra_client.run_ssh_command",
            return_value=(False, "", "not found"),
        ):
            result = await get_container_logs("1.2.3.4", "myapp")

        assert result["success"] is False


class TestGetContainerStatus:
    @pytest.mark.asyncio
    async def test_not_found(self):
        with patch(
            "shared.clients.infra_client.run_ssh_command",
            return_value=(True, "not_found", ""),
        ):
            result = await get_container_status("1.2.3.4", "myapp")

        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_running(self):
        with patch(
            "shared.clients.infra_client.run_ssh_command",
            return_value=(True, "running|healthy|2026-01-01T00:00:00Z", ""),
        ):
            result = await get_container_status("1.2.3.4", "myapp")

        assert result["status"] == "running"
        assert result["health"] == "healthy"


class TestGetContainerStats:
    @pytest.mark.asyncio
    async def test_stats(self):
        with patch(
            "shared.clients.infra_client.run_ssh_command",
            return_value=(True, "5.20%|128MiB / 512MiB", ""),
        ):
            result = await get_container_stats("1.2.3.4", "myapp")

        assert result["cpu_percent"] == 5.2
        assert result["memory_mb"] == 128
        assert result["memory_limit_mb"] == 512


class TestCheckHttpHealth:
    @pytest.mark.asyncio
    async def test_healthy(self):
        import httpx
        import respx

        with respx.mock:
            respx.get("http://1.2.3.4:8080/health").mock(return_value=httpx.Response(200))
            result = await check_http_health("http://1.2.3.4:8080/health")

        assert result["healthy"] is True
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        import httpx
        import respx

        with respx.mock:
            respx.get("http://1.2.3.4:8080/health").mock(return_value=httpx.Response(500))
            result = await check_http_health("http://1.2.3.4:8080/health")

        assert result["healthy"] is False
        assert result["status_code"] == 500
