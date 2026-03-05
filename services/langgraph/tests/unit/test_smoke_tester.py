"""Unit tests for SmokeTesterNode."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.subgraphs.devops.smoke import SmokeTesterNode


@pytest.fixture
def smoke_node():
    return SmokeTesterNode()


def _make_state(
    *,
    modules=None,
    allocated_resources=None,
    deployed_url="http://1.2.3.4:8000",
    resolved_secrets=None,
):
    """Helper to build a minimal DevOpsState dict for smoke tests."""
    if modules is None:
        modules = ["backend"]
    if allocated_resources is None:
        allocated_resources = {
            "srv1:8000": {
                "server_ip": "1.2.3.4",
                "port": 8000,
                "service_name": "backend",
            }
        }
    return {
        "messages": [],
        "project_id": "test-project",
        "project_spec": {"config": {"modules": modules}},
        "allocated_resources": allocated_resources,
        "repo_info": None,
        "provided_secrets": {},
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": resolved_secrets or {},
        "missing_user_secrets": [],
        "deployment_result": {"status": "success"},
        "deployed_url": deployed_url,
        "errors": [],
        "smoke_result": None,
    }


class TestSmokeTesterBackendPass:
    """Backend health check returns 200."""

    async def test_pass_on_200(self, smoke_node):
        state = _make_state()
        mock_response = AsyncMock()
        mock_response.status_code = 200

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        assert len(result["smoke_result"]["checks"]) == 1
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "backend"
        assert check["result"] == "pass"
        assert "errors" not in result or result["errors"] == []


class TestSmokeTesterBackendFail:
    """Backend health check returns non-200."""

    async def test_fail_on_500(self, smoke_node):
        state = _make_state()
        mock_response = AsyncMock()
        mock_response.status_code = 500

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "backend"
        assert check["result"] == "fail"
        assert len(result["errors"]) > 0


class TestSmokeTesterBackendTimeout:
    """Backend health check times out after retries."""

    async def test_timeout(self, smoke_node):
        state = _make_state()

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
            mock_client_cls.return_value = mock_client

            with patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock):
                result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "timeout" in check["detail"].lower() or "connect" in check["detail"].lower()


class TestSmokeTesterRetryLogic:
    """Verify retry logic: fail first, pass on retry."""

    async def test_retries_then_passes(self, smoke_node):
        state = _make_state()

        fail_response = AsyncMock()
        fail_response.status_code = 500
        pass_response = AsyncMock()
        pass_response.status_code = 200

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=[fail_response, pass_response])
            mock_client_cls.return_value = mock_client

            with patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock):
                result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"


class TestSmokeTesterNoModules:
    """No modules to check — vacuous pass."""

    async def test_empty_modules(self, smoke_node):
        state = _make_state(modules=[], allocated_resources={})

        result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        assert result["smoke_result"]["checks"] == []
