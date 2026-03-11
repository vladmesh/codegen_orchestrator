"""Unit tests for deploy pre-check — SSH validation before deploy (#21)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    """Patch api_client methods used by the deploy worker."""
    with patch("src.consumers.deploy.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value=[])  # _check_duplicate_deploy
        api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "test-project",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        api.get_server_ssh_key = AsyncMock(return_value="fake-ssh-key")
        api.get_project_allocations = AsyncMock(return_value=[])
        yield api


def _mock_ssh_conn(exit_status: int):
    """Create a mock asyncssh connection with given test -d exit status."""
    mock_conn = AsyncMock()
    mock_result = MagicMock()
    mock_result.exit_status = exit_status
    mock_result.stdout = ""
    mock_conn.run = AsyncMock(return_value=mock_result)
    return mock_conn


def _patch_asyncssh(exit_status: int):
    """Patch asyncssh module with a mock that returns given exit status."""
    mock_conn = _mock_ssh_conn(exit_status)

    @asynccontextmanager
    async def fake_connect(*args, **kwargs):
        yield mock_conn

    mock_ssh = MagicMock()
    mock_ssh.connect = fake_connect
    mock_ssh.import_private_key = MagicMock(return_value="key-obj")
    return patch("src.consumers.deploy.asyncssh", mock_ssh)


class TestPreCheckServer:
    """Test _pre_check_server SSH directory validation."""

    @pytest.mark.asyncio
    async def test_create_dir_absent_ok(self):
        """create action + dir absent → no error (safe to deploy)."""
        from src.consumers.deploy import _pre_check_server

        with _patch_asyncssh(exit_status=1):  # dir does not exist
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="create",
            )

        assert error is None

    @pytest.mark.asyncio
    async def test_create_dir_exists_error(self):
        """create action + dir exists → error (needs cleanup)."""
        from src.consumers.deploy import _pre_check_server

        with _patch_asyncssh(exit_status=0):  # dir exists
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="create",
            )

        assert error is not None
        assert "already exists" in error

    @pytest.mark.asyncio
    async def test_feature_dir_exists_ok(self):
        """feature action + dir exists → no error (ready to update)."""
        from src.consumers.deploy import _pre_check_server

        with _patch_asyncssh(exit_status=0):  # dir exists
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="feature",
            )

        assert error is None

    @pytest.mark.asyncio
    async def test_feature_dir_absent_error(self):
        """feature action + dir absent → error (never deployed)."""
        from src.consumers.deploy import _pre_check_server

        with _patch_asyncssh(exit_status=1):  # dir does not exist
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="feature",
            )

        assert error is not None
        assert "not found" in error

    @pytest.mark.asyncio
    async def test_fix_same_as_feature(self):
        """fix action behaves like feature (dir must exist)."""
        from src.consumers.deploy import _pre_check_server

        with _patch_asyncssh(exit_status=0):  # dir exists
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="fix",
            )

        assert error is None

    @pytest.mark.asyncio
    async def test_ssh_connection_failure_returns_error(self):
        """SSH connection failure returns descriptive error, doesn't raise."""
        from src.consumers.deploy import _pre_check_server

        mock_ssh = MagicMock()
        mock_ssh.import_private_key = MagicMock(side_effect=ValueError("bad key"))

        with patch("src.consumers.deploy.asyncssh", mock_ssh):
            error = await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_key="fake-key",
                project_name="my-app",
                action="create",
            )

        assert error is not None
        assert "SSH" in error or "ssh" in error.lower()


class TestDeployPreCheckIntegration:
    """Test that deploy_worker.process_deploy_job calls pre-check and aborts on failure."""

    @pytest.mark.asyncio
    @patch("src.consumers.deploy.create_devops_subgraph")
    @patch("src.tools.allocator.ensure_project_allocations", new_callable=AsyncMock)
    @patch("src.consumers.deploy._pre_check_server", new_callable=AsyncMock)
    async def test_precheck_failure_aborts_deploy(
        self, mock_precheck, mock_alloc, mock_devops, mock_redis, mock_api
    ):
        """Pre-check failure should abort deploy before running DevOps subgraph."""
        mock_alloc.return_value = {
            "srv-1:8080": {
                "server_handle": "srv-1",
                "server_ip": "1.2.3.4",
                "port": 8080,
                "service_name": "backend",
            }
        }
        mock_precheck.return_value = "Service dir /opt/services/test-project/ already exists"

        from src.consumers.deploy import process_deploy_job

        job_data = {
            "task_id": "deploy-1",
            "project_id": "proj-1",
            "user_id": "u1",
            "action": "create",
        }

        result = await process_deploy_job(job_data, mock_redis)

        assert result["status"] == "failed"
        assert "already exists" in result["error"]
        mock_devops.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.deploy.create_devops_subgraph")
    @patch("src.tools.allocator.ensure_project_allocations", new_callable=AsyncMock)
    @patch("src.consumers.deploy._pre_check_server", new_callable=AsyncMock)
    async def test_precheck_ok_proceeds_to_deploy(
        self, mock_precheck, mock_alloc, mock_devops, mock_redis, mock_api
    ):
        """Pre-check OK should proceed to DevOps subgraph."""
        mock_alloc.return_value = {
            "srv-1:8080": {
                "server_handle": "srv-1",
                "server_ip": "1.2.3.4",
                "port": 8080,
                "service_name": "backend",
            }
        }
        mock_precheck.return_value = None  # no error

        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={"deployed_url": "http://1.2.3.4:8080", "smoke_result": None}
        )
        mock_devops.return_value = mock_subgraph

        from src.consumers.deploy import process_deploy_job

        job_data = {
            "task_id": "deploy-1",
            "project_id": "proj-1",
            "user_id": "u1",
            "action": "feature",
        }

        result = await process_deploy_job(job_data, mock_redis)

        assert result["status"] == "success"
        mock_devops.assert_called_once()
