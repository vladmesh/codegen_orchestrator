"""Unit tests for DeployerNode."""

from datetime import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.factories import make_repository

from src.subgraphs.devops.deployer import DeployerNode


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
    mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
    mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
    mock_api.create_service_deployment = AsyncMock(return_value={})
    mock_api.create_deployment = AsyncMock(return_value={})
    mock_api.get_primary_repository = AsyncMock(return_value=make_repository(id="repo-test1"))
    mock_api.get_or_create_application = AsyncMock(return_value={"id": 1})
    mock_api.update_application = AsyncMock(return_value={})
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
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_deploy_fails_when_ssh_key_missing(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Deploy should fail when no SSH key is stored for the server."""
        mock_api.get_server_ssh_key = AsyncMock(return_value=None)
        mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "SSH key" in result["errors"][0]

    @pytest.mark.asyncio
    @patch.dict(os.environ, {}, clear=True)
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
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
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_writes_deploy_secrets(self, mock_api, mock_gh_cls, deployer, base_state):
        """set_repository_secrets should be called with DOTENV, DEPLOY_HOST, registry creds, etc."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        gh.set_repository_secrets.assert_called_once()
        secrets_arg = gh.set_repository_secrets.call_args[0][2]
        assert "DOTENV" in secrets_arg
        assert secrets_arg["DEPLOY_HOST"] == "10.0.0.1"
        assert secrets_arg["DEPLOY_USER"] == "dev"
        assert secrets_arg["DEPLOY_SSH_KEY"] == "ssh-key-content"
        assert secrets_arg["DEPLOY_PORT"] == "8080"
        assert secrets_arg["PROJECT_NAME"] == "my_project"
        assert secrets_arg["REGISTRY_URL"] == "registry.example.com"
        assert secrets_arg["REGISTRY_USER"] == "testuser"
        assert secrets_arg["REGISTRY_PASSWORD"] == "testpass"  # noqa: S105
        mock_api.get_server.assert_awaited_once_with("srv-1")
        mock_api.get_server_ssh_key.assert_awaited_once_with("srv-1")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_dotenv_contains_codegen_project_id(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """DOTENV secret must include CODEGEN_PROJECT_ID so compose labels can reference it."""
        import base64

        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        secrets_arg = gh.set_repository_secrets.call_args[0][2]
        dotenv_decoded = base64.b64decode(secrets_arg["DOTENV"]).decode()
        assert "CODEGEN_PROJECT_ID=proj-123" in dotenv_decoded

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_triggers_workflow_dispatch(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        gh.trigger_workflow_dispatch.assert_called_once_with("my-org", "my-repo", "deploy.yml")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_waits_for_completion(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        call_kwargs = gh.wait_for_workflow_completion.call_args[1]
        assert call_kwargs["workflow_file"] == "deploy.yml"
        assert "created_after" in call_kwargs
        assert isinstance(call_kwargs["created_after"], datetime)

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_creates_deployment_record_with_sha(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        mock_api.create_deployment.assert_called_once()
        payload = mock_api.create_deployment.call_args[0][0]
        assert payload["deployed_sha"] == "abc123"
        assert payload["result"] == "success"
        assert payload["application_id"] == 1

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_creates_application_on_deploy(self, mock_api, mock_gh_cls, deployer, base_state):
        _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        mock_api.get_or_create_application.assert_called_once_with(
            repo_id="repo-test1",
            server_handle="srv-1",
            service_name="my_project",
        )
        mock_api.update_application.assert_called_once_with(1, {"status": "running"})

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_deployed_url_uses_external_ip(self, mock_api, mock_gh_cls, deployer, base_state):
        """deployed_url should use the external server IP, not docker service name."""
        _setup_happy_mocks(mock_api, mock_gh_cls)

        result = await deployer.run(base_state)

        assert result["deployed_url"] == "http://10.0.0.1:8080"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_result_contains_application_id(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Deployer result should include application_id for QA handoff."""
        _setup_happy_mocks(mock_api, mock_gh_cls)

        result = await deployer.run(base_state)

        assert result["application_id"] == 1

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_no_project_status_update(self, mock_api, mock_gh_cls, deployer, base_state):
        """Deploy should not update project status — Application status is updated instead."""
        _setup_happy_mocks(mock_api, mock_gh_cls)

        await deployer.run(base_state)

        # api_client.patch should NOT be called for project status updates
        mock_api.patch.assert_not_called()


class TestDeployerNodeFailures:
    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_handles_workflow_failure(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = AsyncMock()
        mock_gh_cls.return_value = gh
        mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
        mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        gh.wait_for_workflow_completion.side_effect = RuntimeError(
            "Workflow deploy.yml failed: failure. See: https://github.com/runs/1"
        )
        gh.get_latest_workflow_run.return_value = None  # rerun not possible
        mock_api.patch = AsyncMock(return_value={})

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "failed" in result["errors"][0].lower()
        # No project service_status update — Application status is the source of truth
        mock_api.patch.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_handles_timeout(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = AsyncMock()
        mock_gh_cls.return_value = gh
        mock_api.get_server_ssh_key = AsyncMock(return_value="ssh-key-content")
        mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        gh.wait_for_workflow_completion.side_effect = TimeoutError(
            "Workflow deploy.yml did not complete within 600s"
        )
        gh.get_latest_workflow_run.return_value = None  # rerun not possible
        mock_api.patch = AsyncMock(return_value={})

        result = await deployer.run(base_state)

        assert result["errors"]
        assert "timeout" in result["errors"][0].lower()
        mock_api.patch.assert_not_called()
