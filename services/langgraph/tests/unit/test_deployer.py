"""Unit tests for DeployerNode."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.clients.github import deploy_pin_tag
from shared.contracts.dto.deploy_dispatch import DeployDispatchClaim
from shared.contracts.dto.run import RunStatus
from src.subgraphs.devops.deployer import DeployerNode
from tests.unit.factories import make_repository

PINNED_SHA = "c" * 40
PIN_TAG = deploy_pin_tag(PINNED_SHA)


class WorkflowCancelledError(RuntimeError):
    """Test double for the cancellation raised by the GitHub client."""


class WorkflowCancellationUnprovenError(RuntimeError):
    """Test double for a cancellation that teardown cannot verify."""


@pytest.fixture
def deployer():
    return DeployerNode()


@pytest.fixture
def base_state():
    return {
        "project_id": "proj-123",
        "project_spec": {
            "title": "My Project",
            "slug": "my-project-0000",
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
                "service_name": "backend",
            }
        },
        "non_secret_values": {"DB_HOST": "localhost", "DB_PORT": "5432"},
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
    mock_api.claim_deploy_dispatch = AsyncMock(
        side_effect=lambda run_id: DeployDispatchClaim(
            run_id=run_id,
            granted=True,
            run_status=RunStatus.RUNNING,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
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
        assert secrets_arg["PROJECT_NAME"] == "my-project-0000"
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
    async def test_cancelled_run_stops_actions_polling_without_rerun(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = WorkflowCancelledError("cancelled")

        result = await deployer.run({**base_state, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.get_latest_workflow_run.assert_not_called()

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
            service_name="my-project-0000",
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
    async def test_deployed_url_uses_backend_allocation_when_tg_bot_comes_first(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        _setup_happy_mocks(mock_api, mock_gh_cls)
        state = {
            **base_state,
            "allocated_resources": {
                "tg_bot": {
                    "server_handle": "srv-1",
                    "server_ip": "10.0.0.1",
                    "port": 8099,
                    "service_name": "tg_bot",
                },
                "backend": {
                    "server_handle": "srv-1",
                    "server_ip": "10.0.0.1",
                    "port": 8080,
                    "service_name": "backend",
                },
            },
        }

        result = await deployer.run(state)

        assert result["deployed_url"] == "http://10.0.0.1:8080"
        secrets_arg = mock_gh_cls.return_value.set_repository_secrets.call_args[0][2]
        assert secrets_arg["DEPLOY_PORT"] == "8080"

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
    async def test_propagates_unproven_workflow_cancellation_without_rerun(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = WorkflowCancellationUnprovenError(
            "could not identify"
        )

        with pytest.raises(WorkflowCancellationUnprovenError):
            await deployer.run(base_state)

        gh.get_latest_workflow_run.assert_not_called()

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


def _pinned_run(head_sha=PINNED_SHA):
    return {**_SUCCESS_RUN, "head_sha": head_sha}


@patch.dict(
    os.environ,
    {
        "ORCHESTRATOR_HOSTNAME": "registry.example.com",
        "REGISTRY_USER": "testuser",
        "REGISTRY_PASSWORD": "testpass",  # noqa: S105
    },
)
class TestDeployerPinnedToCommit:
    """Deploying one named commit instead of whatever main holds now."""

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_dispatches_a_tag_at_the_requested_commit(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """workflow_dispatch rejects a bare SHA, so the commit is pinned by a tag."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.return_value = _pinned_run()

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        gh.create_or_reset_tag.assert_awaited_once_with("my-org", "my-repo", PIN_TAG, PINNED_SHA)
        assert gh.trigger_workflow_dispatch.call_args[1]["ref"] == PIN_TAG
        wait_kwargs = gh.wait_for_workflow_completion.call_args[1]
        assert wait_kwargs["branch"] == PIN_TAG
        assert wait_kwargs["head_sha"] == PINNED_SHA
        assert result["deployment_result"]["status"] == "success"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_records_the_requested_commit_as_deployed(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.return_value = _pinned_run()

        await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert mock_api.create_deployment.call_args[0][0]["deployed_sha"] == PINNED_SHA

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_is_removed_after_a_successful_run(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.return_value = _pinned_run()

        await deployer.run({**base_state, "head_sha": PINNED_SHA})

        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_is_removed_after_a_failed_run(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = None  # rerun not possible

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["errors"]
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_is_removed_after_a_cancelled_run(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = WorkflowCancelledError("cancelled")

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_is_removed_after_an_unproven_cancellation(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = WorkflowCancellationUnprovenError("unproven")

        with pytest.raises(WorkflowCancellationUnprovenError):
            await deployer.run({**base_state, "head_sha": PINNED_SHA})

        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_run_on_another_commit_is_refused_not_reported_as_success(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.return_value = _pinned_run(head_sha="d" * 40)

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "failed"
        assert PINNED_SHA in result["errors"][0]
        mock_api.create_deployment.assert_not_called()
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_rerun_stays_on_the_pinned_tag(self, mock_api, mock_gh_cls, deployer, base_state):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        gh.wait_for_run_completion.return_value = _pinned_run()

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "success"
        rerun_lookup = gh.get_latest_workflow_run.call_args
        assert rerun_lookup[0][3] == PIN_TAG
        assert rerun_lookup[1]["head_sha"] == PINNED_SHA
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_rerun_on_another_commit_is_refused(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        gh.wait_for_run_completion.return_value = _pinned_run(head_sha="d" * 40)

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "failed"
        mock_api.create_deployment.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_surviving_tag_refuses_the_deploy_instead_of_logging_it(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """A pin tag left in the user's repo is a failed deploy, not a successful one."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.return_value = _pinned_run()
        gh.delete_ref.side_effect = RuntimeError("502 from GitHub")

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "failed"
        assert PIN_TAG in result["errors"][0]
        mock_api.create_deployment.assert_not_called()
        mock_api.update_application.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_removal_is_attempted_when_the_create_call_itself_fails(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """GitHub may have applied the ref before the call failed, so cleanup still runs."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.create_or_reset_tag.side_effect = RuntimeError("connection reset after PATCH")

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "failed"
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")
        gh.trigger_workflow_dispatch.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_tag_removal_is_attempted_when_the_create_call_is_cancelled(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.create_or_reset_tag.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await deployer.run({**base_state, "head_sha": PINNED_SHA})

        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_rerun_wait_watches_for_cancellation(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """The rerun is live work: teardown has to reach it, not only the first run."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        gh.wait_for_run_completion.return_value = _pinned_run()

        await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        cancel_check = gh.wait_for_run_completion.call_args[1]["cancel_check"]
        mock_api.get.return_value = {"status": "cancelled"}
        assert await cancel_check() is True
        assert mock_api.get.call_args[0][0] == "runs/deploy-1"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_cancellation_during_rerun_is_not_downgraded_to_a_failed_deploy(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        gh.wait_for_run_completion.side_effect = WorkflowCancelledError("rerun cancelled")

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")
        mock_api.create_deployment.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_unproven_cancellation_during_rerun_keeps_failing_closed(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        gh.wait_for_run_completion.side_effect = WorkflowCancellationUnprovenError("unproven")

        with pytest.raises(WorkflowCancellationUnprovenError):
            await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_deploy_without_a_commit_touches_no_tags(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """The plain 'deploy current main' path is unchanged."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)

        result = await deployer.run(base_state)

        gh.create_or_reset_tag.assert_not_called()
        gh.delete_ref.assert_not_called()
        gh.trigger_workflow_dispatch.assert_called_once_with("my-org", "my-repo", "deploy.yml")
        assert gh.wait_for_workflow_completion.call_args[1]["branch"] == "main"
        assert gh.wait_for_workflow_completion.call_args[1]["head_sha"] is None
        assert result["deployment_result"]["status"] == "success"


@patch.dict(
    os.environ,
    {
        "ORCHESTRATOR_HOSTNAME": "registry.example.com",
        "REGISTRY_USER": "testuser",
        "REGISTRY_PASSWORD": "testpass",  # noqa: S105
    },
)
class TestDeployerFencesEarlierRuns:
    """A deploy that removes a value must outlive every run that could restore it."""

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_earlier_runs_are_stopped_before_this_one_writes(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Secrets are written for the fenced deploy only once nothing else can act.

        Writing first would hand the value to a run that is already going.
        """
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        order = []
        gh.fence_workflow = AsyncMock(side_effect=lambda *a, **k: order.append("fence") or [7])
        gh.set_repository_secrets = AsyncMock(
            side_effect=lambda *a, **k: order.append("secrets") or True
        )
        gh.trigger_workflow_dispatch = AsyncMock(
            side_effect=lambda *a, **k: order.append("dispatch") or True
        )

        result = await deployer.run({**base_state, "fence_active_deploys": True})

        assert order == ["fence", "secrets", "dispatch"]
        gh.fence_workflow.assert_awaited_once_with("my-org", "my-repo", "deploy.yml")
        assert result["deployment_result"]["status"] == "success"

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_an_unstoppable_earlier_run_refuses_the_deploy(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """The grant-after-revoke ordering: an older run that may still write.

        Reporting this deploy successful would record the value as removed while
        the run that set it is still able to put it back, so the deploy fails and
        nothing is dispatched.
        """
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.fence_workflow = AsyncMock(
            side_effect=WorkflowCancellationUnprovenError("run 7 could not be proven terminal")
        )

        result = await deployer.run({**base_state, "fence_active_deploys": True})

        assert result["deployment_result"]["status"] == "failed"
        assert "could not be proven stopped" in result["deployment_result"]["error"]
        gh.set_repository_secrets.assert_not_called()
        gh.trigger_workflow_dispatch.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_an_ordinary_deploy_does_not_fence(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Only a deploy that has to be last pays for the fence."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.fence_workflow = AsyncMock()

        await deployer.run(base_state)

        gh.fence_workflow.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_fenced_out_run_is_not_rerun(self, mock_api, mock_gh_cls, deployer, base_state):
        """The other side of the fence: the deploy that was stopped stays stopped.

        Rerunning it would put back exactly the value the deploy that fenced it
        removed, which is the ordering this whole mechanism exists to exclude.
        """
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        gh.wait_for_workflow_completion.side_effect = WorkflowCancelledError("run 7 was cancelled")

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA})

        assert result["deployment_result"]["status"] == "cancelled"
        gh.rerun_failed_jobs.assert_not_called()


class TestDispatchBoundary:
    """A cancelled run must not reach GitHub, however late the cancellation is.

    The plain status read before the dispatch is not enough on its own: between
    reading it and calling GitHub, a revoke can cancel the run, find no Actions
    run to fence, clear the value, and only then does this deploy write it back.
    The claim is taken against the same locked row as that cancellation, so one
    of the two loses.
    """

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_refused_claim_stops_the_deploy_before_it_dispatches(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        # Not cancelled at any earlier check: the cancellation lands exactly in
        # the window between the last check and the dispatch.
        mock_api.get = AsyncMock(return_value={"status": "running"})
        mock_api.claim_deploy_dispatch = AsyncMock(
            return_value=DeployDispatchClaim(
                run_id="deploy-1", granted=False, run_status=RunStatus.CANCELLED
            )
        )

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.trigger_workflow_dispatch.assert_not_called()
        gh.wait_for_workflow_completion.assert_not_called()
        mock_api.create_deployment.assert_not_called()
        # The pin tag was created before the claim, so it is still cleaned up.
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_the_claim_is_taken_immediately_before_the_dispatch(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """Nothing between the two: any gap is a window the fence cannot cover."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.return_value = _pinned_run()

        order = []
        mock_api.claim_deploy_dispatch.side_effect = lambda run_id: (
            order.append("claim")
            or DeployDispatchClaim(run_id=run_id, granted=True, run_status=RunStatus.RUNNING)
        )
        gh.create_or_reset_tag.side_effect = lambda *a: order.append("tag")
        gh.trigger_workflow_dispatch.side_effect = lambda *a, **kw: order.append("dispatch")

        await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert order == ["tag", "claim", "dispatch"]

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_rerun_asks_again_before_restarting_the_workflow(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """A rerun is the same external effect, so it crosses the same boundary."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        claims = [
            DeployDispatchClaim(run_id="deploy-1", granted=True, run_status=RunStatus.RUNNING),
            DeployDispatchClaim(run_id="deploy-1", granted=False, run_status=RunStatus.CANCELLED),
        ]
        mock_api.claim_deploy_dispatch = AsyncMock(side_effect=claims)

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.rerun_failed_jobs.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_claim_held_past_its_deadline_does_not_dispatch(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """The promise that lets reconciliation stop waiting for a silent worker.

        Once the deadline has gone by, the claim may have been taken back and a
        revoke may already have cleared the value on the strength of nothing more
        appearing on Actions. Dispatching now would put the identity back on an
        application whose grant is recorded revoked, so this stops instead.
        """
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        mock_api.claim_deploy_dispatch = AsyncMock(
            return_value=DeployDispatchClaim(
                run_id="deploy-1",
                granted=True,
                run_status=RunStatus.RUNNING,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.trigger_workflow_dispatch.assert_not_called()
        gh.delete_ref.assert_awaited_once_with("my-org", "my-repo", f"tags/{PIN_TAG}")

    @pytest.mark.asyncio
    @patch("src.subgraphs.devops.deployer.GitHubAppClient")
    @patch("src.subgraphs.devops.deployer.api_client")
    async def test_a_rerun_stops_once_its_renewed_claim_has_expired(
        self, mock_api, mock_gh_cls, deployer, base_state
    ):
        """A rerun restarts the same effect, so the same deadline binds it."""
        gh = _setup_happy_mocks(mock_api, mock_gh_cls)
        mock_api.get = AsyncMock(return_value={"status": "running"})
        gh.wait_for_workflow_completion.side_effect = RuntimeError("Workflow deploy.yml failed")
        gh.get_latest_workflow_run.return_value = _pinned_run()
        claims = [
            DeployDispatchClaim(
                run_id="deploy-1",
                granted=True,
                run_status=RunStatus.RUNNING,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            DeployDispatchClaim(
                run_id="deploy-1",
                granted=True,
                run_status=RunStatus.RUNNING,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ]
        mock_api.claim_deploy_dispatch = AsyncMock(side_effect=claims)

        result = await deployer.run({**base_state, "head_sha": PINNED_SHA, "run_id": "deploy-1"})

        assert result["deployment_result"] == {"status": "cancelled"}
        gh.rerun_failed_jobs.assert_not_called()
