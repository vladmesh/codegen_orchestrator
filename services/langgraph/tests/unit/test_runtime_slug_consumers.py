"""Runtime slug invariant across deploy consumers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.factories import make_project

from shared.contracts.queues.deploy import DeployAction
from src.consumers._qa_runner import run_qa_on_server
from src.consumers.deploy import _build_subgraph_input
from src.consumers.deploy_lifecycle import process_lifecycle_action
from src.consumers.deploy_precheck import _pre_check_server
from src.subgraphs.devops.secret_resolver import SecretResolverNode
from src.subgraphs.devops.smoke import SmokeTesterNode

RUNTIME_SLUG = "fancy-project-with-spaces-0000"
DISPLAY_TITLE = "Fancy_Project With Spaces"
SERVICE_DIR = f"/opt/services/{RUNTIME_SLUG}"


def _ssh_module_for_connection(mock_conn: AsyncMock) -> MagicMock:
    mock_ssh = MagicMock()
    mock_ssh.import_private_key = MagicMock(return_value="key-obj")
    mock_ssh.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return mock_ssh


@pytest.mark.asyncio
async def test_runtime_consumers_resolve_same_slug_dir_and_compose_project():
    project = make_project(title=DISPLAY_TITLE, slug=RUNTIME_SLUG)
    deploy_input = _build_subgraph_input(
        project_id=str(project.id),
        project=project,
        git_url=f"https://github.com/org/{RUNTIME_SLUG}",
        allocated_resources={},
        job_data={"task_id": "deploy-1"},
    )
    project_spec = deploy_input["project_spec"]

    assert project_spec["title"] == DISPLAY_TITLE
    assert project_spec["slug"] == RUNTIME_SLUG

    resolver = SecretResolverNode()
    assert resolver._compute_secret("COMPOSE_PROJECT_NAME", project_spec, {}) == RUNTIME_SLUG
    assert resolver._compute_secret("PROJECT_NAME", project_spec, {}) == RUNTIME_SLUG

    precheck_conn = AsyncMock()
    precheck_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout=""))

    @asynccontextmanager
    async def precheck_connect(*args, **kwargs):
        yield precheck_conn

    precheck_ssh = MagicMock()
    precheck_ssh.import_private_key = MagicMock(return_value="key-obj")
    precheck_ssh.connect = precheck_connect

    with patch("src.consumers.deploy_precheck.asyncssh", precheck_ssh):
        assert (
            await _pre_check_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="fake-key",
                project_name=RUNTIME_SLUG,
                action="feature",
            )
            is None
        )
    assert precheck_conn.run.await_args.args[0] == f"test -d {SERVICE_DIR}/"

    lifecycle_conn = AsyncMock()
    lifecycle_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="stopped"))
    with (
        patch("src.consumers.deploy_lifecycle.api_client") as lifecycle_api,
        patch(
            "src.consumers.deploy_lifecycle.asyncssh",
            _ssh_module_for_connection(lifecycle_conn),
        ),
    ):
        lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
        result = await process_lifecycle_action(
            action=DeployAction.STOP,
            task_id="deploy-1",
            project_id=str(project.id),
            project_name=RUNTIME_SLUG,
            allocated_resources={
                "srv-1:8000": {"server_ip": "1.2.3.4", "server_handle": "srv-1"}
            },
        )
    assert result["status"] == "success"
    lifecycle_cmd = lifecycle_conn.run.await_args.args[0]
    assert f"cd {SERVICE_DIR}/infra" in lifecycle_cmd
    assert f"docker compose -p {RUNTIME_SLUG}" in lifecycle_cmd

    smoke_conn = AsyncMock()
    smoke_conn.run = AsyncMock(return_value=MagicMock(stdout="backend failed\n"))
    with (
        patch("src.subgraphs.devops.smoke.api_client") as smoke_api,
        patch("src.subgraphs.devops.smoke.asyncssh", _ssh_module_for_connection(smoke_conn)),
    ):
        smoke_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        smoke_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
        logs = await SmokeTesterNode()._fetch_container_logs(
            server_ip="1.2.3.4",
            server_handle="srv-1",
            project_name=RUNTIME_SLUG,
        )
    assert logs == "backend failed"
    smoke_cmd = smoke_conn.run.await_args.args[0]
    assert f"cd {SERVICE_DIR}" in smoke_cmd
    assert f"docker compose -p {RUNTIME_SLUG}" in smoke_cmd

    qa_conn = AsyncMock()
    qa_conn.run = AsyncMock(
        side_effect=[
            MagicMock(exit_status=0, stdout='{"pass": true, "checks": [], "summary": "ok"}'),
            MagicMock(exit_status=1, stdout=""),
        ]
    )
    with (
        patch("src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock),
        patch("src.consumers._qa_runner.asyncssh", _ssh_module_for_connection(qa_conn)),
    ):
        qa_result = await run_qa_on_server(
            server_ip="1.2.3.4",
            ssh_user="dev",
            ssh_key="fake-key",
            project_name=RUNTIME_SLUG,
            acceptance_criteria="- GET /health returns 200",
            deployed_url="http://1.2.3.4:8000",
        )
    assert qa_result.passed is True
    qa_cmd = qa_conn.run.await_args_list[0].args[0]
    assert f"cd {SERVICE_DIR}" in qa_cmd
