"""Runtime slug invariant across deploy consumers."""

from __future__ import annotations

from contextlib import asynccontextmanager
import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.deploy import DeployAction
from shared.live_harness_cleanup import REMOTE_CLEANUP_SCRIPT
from src.consumers._qa_target import QATarget, QATargetSession, resolve_capabilities
from src.consumers.deploy import _build_subgraph_input
from src.consumers.deploy_lifecycle import process_lifecycle_action
from src.consumers.deploy_precheck import _pre_check_server
from src.subgraphs.devops.secret_resolver import SecretResolverNode
from src.subgraphs.devops.smoke import SmokeTesterNode
from tests.unit.factories import make_project

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
        head_sha="a" * 40,
        deployed_commit_sha="e" * 40,
        fence_active_deploys=False,
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
            server_handle="srv-1",
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

    # QA reaches the target through a typed session, not a shell. The slug is
    # what scopes that session: the deployment directory it may read, and the
    # container names it recognises as belonging to this run.
    qa_target = QATarget(
        server_ip="1.2.3.4",
        ssh_user="dev",
        qa_ssh_user="qa-observer",
        server_handle="vps-1",
        project_name=RUNTIME_SLUG,
        deployed_url="http://1.2.3.4:8000",
    )
    assert qa_target.service_dir == SERVICE_DIR
    qa_conn = AsyncMock()
    qa_conn.run = AsyncMock(
        side_effect=[
            MagicMock(exit_status=0, stdout=f"{SERVICE_DIR}\n", stderr=""),
            MagicMock(exit_status=0, stdout=f"{RUNTIME_SLUG}-backend-1\n", stderr=""),
        ]
    )
    qa_capabilities = await resolve_capabilities(qa_conn, qa_target)
    # The slug is what scopes the run: it names the directory whose physical
    # root bounds every read, and the compose project whose containers the run
    # may inspect.
    assert f"readlink -f -- {SERVICE_DIR}" in qa_conn.run.await_args_list[0].args[0]
    assert (
        f"label=com.docker.compose.project={RUNTIME_SLUG}" in qa_conn.run.await_args_list[1].args[0]
    )
    assert qa_capabilities.physical_root == SERVICE_DIR
    qa_session = QATargetSession(qa_target, qa_conn, qa_capabilities)
    assert qa_session.check_container(f"{RUNTIME_SLUG}-backend-1") == f"{RUNTIME_SLUG}-backend-1"

    unsafe_project = "unsafe project; echo nope"
    unsafe_conn = AsyncMock()
    unsafe_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="stopped"))
    with (
        patch("src.consumers.deploy_lifecycle.api_client") as lifecycle_api,
        patch(
            "src.consumers.deploy_lifecycle.asyncssh",
            _ssh_module_for_connection(unsafe_conn),
        ),
    ):
        lifecycle_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        lifecycle_api.get_server_ssh_key = AsyncMock(return_value="fake-key")
        await process_lifecycle_action(
            action=DeployAction.UNDEPLOY,
            task_id="deploy-1",
            project_id=str(project.id),
            project_name=unsafe_project,
            server_handle="srv-1",
        )
    unsafe_cmd = unsafe_conn.run.await_args.args[0]
    assert shlex.split(unsafe_cmd) == ["sh", "-s", "--", unsafe_project, "/opt/services"]
    assert unsafe_conn.run.await_args.kwargs["input"] == REMOTE_CLEANUP_SCRIPT.read_text()
