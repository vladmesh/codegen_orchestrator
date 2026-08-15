"""Deploy lifecycle actions: stop, undeploy.

Simple SSH operations that skip the full DevOps subgraph.
"""

from __future__ import annotations

import shlex

import asyncssh
import structlog

from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.live_harness_cleanup import REMOTE_CLEANUP_SCRIPT, build_remote_cleanup_command

from ..clients.api import api_client
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"


async def process_lifecycle_action(
    *,
    action: DeployAction,
    task_id: str,
    project_id: str,
    project_name: str,
    server_handle: str,
) -> dict:
    """Execute a stop or undeploy action via SSH on the application's own server.

    The server comes from the application the caller is bringing down, not from the
    project: a project with applications on several servers must reach the one it
    was asked about.

    Returns:
        Result dict with status and details.
    """
    server = await api_client.get_server(server_handle)
    server_ip = server.public_ip
    ssh_key = await api_client.get_server_ssh_key(server_handle)
    if not ssh_key:
        return live_work_unsettled(
            {
                "status": "failed",
                "error": f"No SSH key for server {server_handle}",
                "deploy_outcome": DeployOutcome.GIVE_UP.value,
            }
        )

    service_dir = f"{SERVICE_BASE_DIR}/{project_name}"
    compose_cmd = (
        f"cd {shlex.quote(f'{service_dir}/infra')} && "
        f"docker compose -p {shlex.quote(project_name)} "
        f"--env-file ../.env -f compose.base.yml -f compose.prod.yml"
    )

    if action == DeployAction.STOP:
        cmd = f"{compose_cmd} stop"
        remote_input = None
    elif action == DeployAction.UNDEPLOY:
        # Both production and recovery stream this one selector/order to the
        # target. A failure retains the service directory for the next retry.
        cmd = build_remote_cleanup_command(project_name, SERVICE_BASE_DIR)
        remote_input = REMOTE_CLEANUP_SCRIPT.read_text()
    else:
        raise ValueError(f"Unexpected lifecycle action: {action}")

    try:
        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username=server.ssh_user,
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            result = await conn.run(cmd, check=False, input=remote_input)

            if result.exit_status != 0:
                error = f"SSH command failed (exit {result.exit_status}): {result.stderr}"
                logger.error(
                    "deploy_lifecycle_ssh_failed",
                    task_id=task_id,
                    project_id=project_id,
                    server_handle=server_handle,
                    action=action.value,
                    error=error,
                )
                return live_work_unsettled(
                    {
                        "status": "failed",
                        "error": error,
                        "deploy_outcome": DeployOutcome.GIVE_UP.value,
                    }
                )

            logger.info(
                "deploy_lifecycle_success",
                task_id=task_id,
                project_id=project_id,
                server_handle=server_handle,
                action=action.value,
                project_name=project_name,
                server_ip=server_ip,
                output=result.stdout[:500] if result.stdout else "",
            )

            return live_work_settled(
                {
                    "status": "success",
                    "action": action.value,
                    "deploy_outcome": DeployOutcome.SUCCESS.value,
                }
            )

    except Exception as e:
        logger.error(
            "deploy_lifecycle_exception",
            task_id=task_id,
            project_id=project_id,
            server_handle=server_handle,
            action=action.value,
            error=str(e),
            exc_info=True,
        )
        return live_work_unsettled(
            {
                "status": "failed",
                "error": str(e),
                "deploy_outcome": DeployOutcome.GIVE_UP.value,
            }
        )
