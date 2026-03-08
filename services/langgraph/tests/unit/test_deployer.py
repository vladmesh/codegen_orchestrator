"""Unit tests for DeployerNode."""

from datetime import datetime
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.subgraphs.devops.nodes import DeployerNode


@pytest.fixture
def deployer():
    return DeployerNode()


@pytest.fixture
def base_state():
    return {
        "project_id": "proj-123",
        "project_spec": {
            "name": "my project",
            "config": {"modules": ["backend"]},
        },
        "repo_info": {
            "full_name": "my-org/my-repo",
            "html_url": "https://github.com/my-org/my-repo",
        },
        "allocated_resources": {
            "backend": {
                "server_handle": "srv-1",
                "server_ip": "10.0.0.1",
                "port": 8080,
            }
        },
        "resolved_secrets": {"DB_HOST": "localhost", "DB_PORT": "5432"},
        "messages": [],
        "errors": [],
    }


_ALLOC_RESPONSE = [{"server_handle": "srv-1", "port": 8080}]

_SUCCESS_RUN = {
    "id": 1,
    "status": "completed",
    "conclusion": "success",
    "html_url": "https://github.com/runs/1",
    "head_sha": "abc123",
}


def _setup_happy_mocks(mock_api, mock_gh_cls):
    gh = AsyncMock()
    mock_gh_cls.return_value = gh
    gh.wait_for_workflow_completion.return_value = _SUCCESS_RUN
    mock_api.get_project_allocations = AsyncMock(return_value=_ALLOC_RESPONSE)
    mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
    mock_api.create_service_deployment = AsyncMock(return_value={})
    mock_api.patch = AsyncMock(return_value={})
    return gh


class TestDeployerNodeErrors:
    @pytest.mark.asyncio
    async def test_no_project_id_returns_error(self, deployer):
        state = {"project_id": None, "project_spec": {}, "messages": [], "errors": []}
        result = await deployer.run(state)
        assert result["errors"]
        assert "No project_id" in result["errors"][0]

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_deploy_fails_when_ssh_key_missing(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Deploy should fail when no SSH key is stored for the server."""
        mock_api.get_server_ssh_key = AsyncMock(return_value=None)

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "SSH key" in result["errors"][0]

    @pytest.mark.asyncio
    @patch.dict(os.environ, {}, clear=True)
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_deploy_fails_when_registry_env_missing(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Deploy should fail when ORCHESTRATOR_HOSTNAME/REGISTRY_USER/PASSWORD are missing."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        # _write_deploy_secrets should have returned False (secrets not written)
        gh.set_repository_secrets.assert_not_called()


@patch.dict(
    os.environ,
    {
        "ORCHESTRATOR_HOSTNAME": "registry.example.com",
        "REGISTRY_USER": "testuser",
        "REGISTRY_PASSWORD": "testpass",  # noqa: S105
    },
)
class TestDeployerNodeHappyPath:
    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_writes_deploy_secrets(self, mock_api, mock_gh_cls, deployer, base_state):
        """set_repository_secrets should be called with DOTENV, DEPLOY_HOST, registry creds, etc."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        gh.set_repository_secrets.assert_called_once()
        secrets_arg = gh.set_repository_secrets.call_args[0][2]
        assert "DOTENV" in secrets_arg
        assert secrets_arg["DEPLOY_HOST"] == "10.0.0.1"
        assert secrets_arg["DEPLOY_USER"] == "root"
        assert secrets_arg["DEPLOY_SSH_KEY"] == "ssh-key-content"
        assert secrets_arg["DEPLOY_PORT"] == "8080"
        assert secrets_arg["PROJECT_NAME"] == "my_project"
        assert secrets_arg["REGISTRY_URL"] == "registry.example.com"
        assert secrets_arg["REGISTRY_USER"] == "testuser"
        assert secrets_arg["REGISTRY_PASSWORD"] == "testpass"  # noqa: S105

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_triggers_workflow_dispatch(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        gh.trigger_workflow_dispatch.assert_called_once_with("my-org", "my-repo", "deploy.yml")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_waits_for_completion(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        call_kwargs = gh.wait_for_workflow_completion.call_args[1]
        assert call_kwargs["workflow_file"] == "deploy.yml"
        assert "created_after" in call_kwargs
        assert isinstance(call_kwargs["created_after"], datetime)

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_creates_deployment_record_with_sha(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        mock_api.create_service_deployment.assert_called_once()
        payload = mock_api.create_service_deployment.call_args[0][0]
        assert payload["deployed_sha"] == "abc123"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_updates_project_status_to_active(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        mock_api.patch.assert_called_once_with(
            "/projects/proj-123",
            json={"status": "active"},
        )


class TestDeployerNodeFailures:
    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_handles_workflow_failure(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = AsyncMock()
        mock_gh_cls.return_value = gh
        mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
        gh.wait_for_workflow_completion.side_effect = RuntimeError(
            "Workflow deploy.yml failed: failure. See: https://github.com/runs/1"
        )
        gh.get_latest_workflow_run.return_value = None  # rerun not possible
        mock_api.patch = AsyncMock(return_value={})

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "failed" in result["errors"][0].lower()
        mock_api.patch.assert_called_once_with(
            "/projects/proj-123",
            json={"status": "error"},
        )

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.nodes.GitHubAppClient")
    @patch("src.subgraphs.devops.nodes.api_client")
    async def test_handles_timeout(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = AsyncMock()
        mock_gh_cls.return_value = gh
        mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
        gh.wait_for_workflow_completion.side_effect = TimeoutError(
            "Workflow deploy.yml did not complete within 600s"
        )
        gh.get_latest_workflow_run.return_value = None  # rerun not possible
        mock_api.patch = AsyncMock(return_value={})

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "timeout" in result["errors"][0].lower()
        mock_api.patch.assert_called_once_with(
            "/projects/proj-123",
            json={"status": "error"},
        )
