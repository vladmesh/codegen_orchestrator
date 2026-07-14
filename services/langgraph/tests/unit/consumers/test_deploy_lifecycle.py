"""Unit tests for deploy lifecycle actions (stop, undeploy).

Verifies that stop/undeploy actions SSH to server, run correct commands,
and update run status without running the full DevOps subgraph.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger


def _make_job_data(*, action: str, **overrides) -> dict:
    defaults = {
        "task_id": "deploy-lifecycle-1",
        "project_id": "proj-1",
        "user_id": "",
        "story_id": "",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": action,
        "deploy_fix_attempt": 0,
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.delete = AsyncMock()
    redis.publish_flat = AsyncMock()
    redis.publish_message = AsyncMock()
    return redis


class TestDeployLifecycleStop:
    @pytest.mark.asyncio
    async def test_stop_runs_docker_compose_stop(self, mock_redis):
        from src.consumers.deploy import process_deploy_job

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="stopped"))

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
        ):
            mock_api.patch = AsyncMock(return_value={})
            mock_api.get_project = AsyncMock(return_value=MagicMock(name="weather_bot", config={}))
            mock_lifecycle_api.get_primary_repository = AsyncMock(
                return_value=MagicMock(id="repo-1")
            )
            mock_lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
            mock_lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))

            # Mock allocator to return server info
            with patch(
                "src.consumers.deploy._allocate_resources",
                new_callable=AsyncMock,
                return_value={
                    "primary": {
                        "server_ip": "1.2.3.4",
                        "server_handle": "vps-1",
                        "port": 8080,
                        "application_id": 1,
                    }
                },
            ):
                mock_ssh.import_private_key = MagicMock(return_value="key-obj")
                mock_ssh.connect = MagicMock(
                    return_value=AsyncMock(
                        __aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()
                    )
                )

                result = await process_deploy_job(_make_job_data(action="stop"), mock_redis)

            assert result["status"] == "success"
            assert mock_ssh.connect.call_args.kwargs["username"] == "dev"
            # Verify SSH command runs compose from infra/ with correct flags
            ssh_cmd = mock_conn.run.call_args[0][0]
            assert "/infra" in ssh_cmd
            assert "--env-file ../.env" in ssh_cmd
            assert "compose.base.yml" in ssh_cmd
            assert "compose.prod.yml" in ssh_cmd
            assert "stop" in ssh_cmd

    @pytest.mark.asyncio
    async def test_stop_does_not_run_devops_subgraph(self, mock_redis):
        from src.consumers.deploy import process_deploy_job

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="stopped"))

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
            patch("src.consumers.deploy.create_devops_subgraph") as mock_subgraph,
            patch(
                "src.consumers.deploy._allocate_resources",
                new_callable=AsyncMock,
                return_value={
                    "primary": {
                        "server_ip": "1.2.3.4",
                        "server_handle": "vps-1",
                        "port": 8080,
                        "application_id": 1,
                    }
                },
            ),
        ):
            mock_api.patch = AsyncMock(return_value={})
            mock_api.get_project = AsyncMock(return_value=MagicMock(name="weather_bot", config={}))
            mock_lifecycle_api.get_primary_repository = AsyncMock(
                return_value=MagicMock(id="repo-1")
            )
            mock_lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
            mock_lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))

            mock_ssh.import_private_key = MagicMock(return_value="key-obj")
            mock_ssh.connect = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()
                )
            )

            await process_deploy_job(_make_job_data(action="stop"), mock_redis)

            mock_subgraph.assert_not_called()


class TestDeployLifecycleUndeploy:
    @pytest.mark.asyncio
    async def test_undeploy_runs_docker_compose_down(self, mock_redis):
        from src.consumers.deploy import process_deploy_job

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="removed"))

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
            patch(
                "src.consumers.deploy._allocate_resources",
                new_callable=AsyncMock,
                return_value={
                    "primary": {
                        "server_ip": "1.2.3.4",
                        "server_handle": "vps-1",
                        "port": 8080,
                        "application_id": 1,
                    }
                },
            ),
        ):
            mock_api.patch = AsyncMock(return_value={})
            mock_api.get_project = AsyncMock(return_value=MagicMock(name="weather_bot", config={}))
            mock_lifecycle_api.get_primary_repository = AsyncMock(
                return_value=MagicMock(id="repo-1")
            )
            mock_lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
            mock_lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))

            mock_ssh.import_private_key = MagicMock(return_value="key-obj")
            mock_ssh.connect = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()
                )
            )

            result = await process_deploy_job(_make_job_data(action="undeploy"), mock_redis)

        assert result["status"] == "success"
        ssh_cmd = mock_conn.run.call_args[0][0]
        assert "/infra" in ssh_cmd
        assert "--env-file ../.env" in ssh_cmd
        assert "compose.base.yml" in ssh_cmd
        assert "compose.prod.yml" in ssh_cmd
        assert "down -v" in ssh_cmd
        assert "rm -rf /opt/services/" in ssh_cmd


class TestDeployLifecycleSSHFailure:
    @pytest.mark.asyncio
    async def test_ssh_failure_returns_failed(self, mock_redis):
        from src.consumers.deploy import process_deploy_job

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
            patch(
                "src.consumers.deploy._allocate_resources",
                new_callable=AsyncMock,
                return_value={
                    "primary": {
                        "server_ip": "1.2.3.4",
                        "server_handle": "vps-1",
                        "port": 8080,
                        "application_id": 1,
                    }
                },
            ),
        ):
            mock_api.patch = AsyncMock(return_value={})
            mock_api.get_project = AsyncMock(return_value=MagicMock(name="weather_bot", config={}))
            mock_lifecycle_api.get_primary_repository = AsyncMock(
                return_value=MagicMock(id="repo-1")
            )
            mock_lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
            mock_lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))

            mock_ssh.import_private_key = MagicMock(return_value="key-obj")
            mock_ssh.connect = MagicMock(side_effect=ConnectionError("SSH failed"))

            result = await process_deploy_job(_make_job_data(action="stop"), mock_redis)

        assert result["status"] == "failed"
        # Run should be marked as failed
        patch_calls = [c for c in mock_api.patch.call_args_list if "runs/" in str(c)]
        last_run_patch = patch_calls[-1]
        assert last_run_patch[1]["json"]["result"]["deploy_outcome"] == DeployOutcome.GIVE_UP.value
