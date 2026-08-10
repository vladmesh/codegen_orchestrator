"""Unit tests for deploy lifecycle actions (stop, undeploy).

Verifies that stop/undeploy actions SSH to the server of the application the message
names, run the correct commands, and update run and application status without running
the full DevOps subgraph.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger
from tests.unit.factories import make_run, make_run_start


def _make_job_data(*, action: str, **overrides) -> dict:
    defaults = {
        "task_id": "deploy-lifecycle-1",
        "project_id": "proj-1",
        "telegram_chat_id": "987654321",
        "story_id": "",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": action,
        "deploy_fix_attempt": 0,
        "application_id": 1,
    }
    defaults.update(overrides)
    return defaults


def _application(app_id: int, server_handle: str) -> MagicMock:
    return MagicMock(id=app_id, server_handle=server_handle, service_name="weather-bot")


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.delete = AsyncMock()
    redis.publish_flat = AsyncMock()
    redis.publish_message = AsyncMock()
    return redis


@pytest.fixture
def ssh_conn():
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="done"))
    return conn


def _lifecycle_patches(mock_api, mock_lifecycle_api, mock_ssh, ssh_conn, *, applications=None):
    """Wire the doubles a lifecycle run touches. `applications` maps id → server handle."""
    applications = applications or {1: "vps-1"}
    mock_api.patch = AsyncMock(return_value={})
    mock_api.get_run = AsyncMock(return_value=make_run())
    mock_api.start_run = AsyncMock(return_value=make_run_start())
    mock_api.get_project = AsyncMock(
        return_value=MagicMock(title="Weather Bot", slug="weather-bot-0000", config={})
    )
    mock_api.get_application = AsyncMock(
        side_effect=lambda app_id: _application(app_id, applications[app_id])
    )
    mock_lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
    mock_lifecycle_api.get_server = AsyncMock(
        side_effect=lambda handle: MagicMock(ssh_user="dev", public_ip=f"10.0.0.{handle[-1]}")
    )
    mock_ssh.import_private_key = MagicMock(return_value="key-obj")
    mock_ssh.connect = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(return_value=ssh_conn), __aexit__=AsyncMock())
    )


class TestDeployLifecycleStop:
    @pytest.mark.asyncio
    async def test_stop_runs_docker_compose_stop(self, mock_redis, ssh_conn):
        from src.consumers.deploy import process_deploy_job

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
        ):
            _lifecycle_patches(mock_api, mock_lifecycle_api, mock_ssh, ssh_conn)

            result = await process_deploy_job(_make_job_data(action="stop"), mock_redis)

        assert result["status"] == "success"
        assert mock_ssh.connect.call_args.kwargs["username"] == "dev"
        # Verify SSH command runs compose from infra/ with correct flags
        ssh_cmd = ssh_conn.run.call_args[0][0]
        assert "/infra" in ssh_cmd
        assert "--env-file ../.env" in ssh_cmd
        assert "compose.base.yml" in ssh_cmd
        assert "compose.prod.yml" in ssh_cmd
        assert "stop" in ssh_cmd
        assert "cd /opt/services/weather-bot-0000/infra" in ssh_cmd

    @pytest.mark.asyncio
    async def test_stop_does_not_run_devops_subgraph(self, mock_redis, ssh_conn):
        from src.consumers.deploy import process_deploy_job

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
            patch("src.consumers.deploy.create_devops_subgraph") as mock_subgraph,
            patch("src.consumers.deploy._allocate_resources", new_callable=AsyncMock) as mock_alloc,
        ):
            _lifecycle_patches(mock_api, mock_lifecycle_api, mock_ssh, ssh_conn)

            await process_deploy_job(_make_job_data(action="stop"), mock_redis)

            mock_subgraph.assert_not_called()
            # Allocation would create an application on a server of its own choosing.
            mock_alloc.assert_not_called()
            # Lifecycle actions never inspect deployment SHA deduplication state.
            mock_api.get.assert_not_called()


class TestDeployLifecycleUndeploy:
    @pytest.mark.asyncio
    async def test_undeploy_runs_docker_compose_down(self, mock_redis, ssh_conn):
        from src.consumers.deploy import process_deploy_job

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
        ):
            _lifecycle_patches(mock_api, mock_lifecycle_api, mock_ssh, ssh_conn)

            result = await process_deploy_job(_make_job_data(action="undeploy"), mock_redis)

        assert result["status"] == "success"
        ssh_cmd = ssh_conn.run.call_args[0][0]
        assert "/infra" in ssh_cmd
        assert "--env-file ../.env" in ssh_cmd
        assert "compose.base.yml" in ssh_cmd
        assert "compose.prod.yml" in ssh_cmd
        assert "down -v" in ssh_cmd
        assert "rm -rf /opt/services/weather-bot-0000" in ssh_cmd
        mock_api.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_undeploy_hits_the_application_it_names(self, mock_redis):
        """A project on two servers: one undeploy per application, each on its own host.

        This is the teardown case. If the consumer resolved the target itself it would
        answer with the same application both times, leave the other container up, and
        leave a bot polling a token the user was told is free.
        """
        from src.consumers.deploy import process_deploy_job

        connections: dict[str, AsyncMock] = {}

        def connect(server_ip, **_kwargs):
            conn = AsyncMock()
            conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="removed"))
            connections[server_ip] = conn
            return AsyncMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock())

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
        ):
            _lifecycle_patches(
                mock_api,
                mock_lifecycle_api,
                mock_ssh,
                AsyncMock(),
                applications={7: "vps-1", 9: "vps-2"},
            )
            mock_ssh.connect = MagicMock(side_effect=connect)

            for app_id in (7, 9):
                result = await process_deploy_job(
                    _make_job_data(
                        action="undeploy", application_id=app_id, task_id=f"deploy-{app_id}"
                    ),
                    mock_redis,
                )
                assert result["status"] == "success"

        assert sorted(connections) == ["10.0.0.1", "10.0.0.2"]

        patched_apps = {
            call.args[0]: call.kwargs["json"]["status"]
            for call in mock_api.patch.call_args_list
            if call.args[0].startswith("applications/")
        }
        assert patched_apps == {
            "applications/7": ApplicationStatus.NOT_DEPLOYED.value,
            "applications/9": ApplicationStatus.NOT_DEPLOYED.value,
        }


class TestDeployLifecycleSSHFailure:
    @pytest.mark.asyncio
    async def test_ssh_failure_returns_failed(self, mock_redis, ssh_conn):
        from src.consumers.deploy import process_deploy_job

        with (
            patch("src.consumers.deploy.api_client") as mock_api,
            patch("src.consumers.deploy._deploy_lock_ttl", return_value=3600),
            patch("src.consumers.deploy_lifecycle.api_client") as mock_lifecycle_api,
            patch("src.consumers.deploy_lifecycle.asyncssh") as mock_ssh,
        ):
            _lifecycle_patches(mock_api, mock_lifecycle_api, mock_ssh, ssh_conn)
            mock_ssh.connect = MagicMock(side_effect=ConnectionError("SSH failed"))

            result = await process_deploy_job(_make_job_data(action="stop"), mock_redis)

        assert result["status"] == "failed"
        # Run should be marked as failed
        patch_calls = [c for c in mock_api.patch.call_args_list if "runs/" in str(c)]
        last_run_patch = patch_calls[-1]
        assert last_run_patch[1]["json"]["result"]["deploy_outcome"] == DeployOutcome.GIVE_UP.value
        # A failed teardown must not report the application down.
        assert not [c for c in mock_api.patch.call_args_list if "applications/" in str(c)]
