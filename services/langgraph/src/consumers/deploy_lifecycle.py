"""Deploy lifecycle actions: stop, undeploy.

Simple SSH operations that skip the full DevOps subgraph.
"""

from __future__ import annotations

import shlex

import asyncssh
import structlog

from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.runtime_project import runtime_project_slug

from ..clients.api import api_client

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"


async def process_lifecycle_action(
    *,
    action: DeployAction,
    task_id: str,
    project_id: str,
    project_name: str,
    allocated_resources: dict,
) -> dict:
    """Execute a stop or undeploy action via SSH.

    Returns:
        Result dict with status and details.
    """
    first_resource = next(iter(allocated_resources.values()), {})
    server_ip = first_resource.get("server_ip")
    server_handle = first_resource.get("server_handle")

    try:
        project_slug = runtime_project_slug(project_name)
    except ValueError as e:
        return {
            "status": "failed",
            "error": str(e),
            "deploy_outcome": DeployOutcome.GIVE_UP.value,
        }

    if not server_ip or not server_handle:
        return {
            "status": "failed",
            "error": f"No server info in allocated resources for project {project_id}",
            "deploy_outcome": DeployOutcome.GIVE_UP.value,
        }

    server = await api_client.get_server(server_handle)
    ssh_key = await api_client.get_server_ssh_key(server_handle)
    if not ssh_key:
        return {
            "status": "failed",
            "error": f"No SSH key for server {server_handle}",
            "deploy_outcome": DeployOutcome.GIVE_UP.value,
        }

    service_dir = f"{SERVICE_BASE_DIR}/{project_slug}"
    quoted_service_dir = shlex.quote(service_dir)
    compose_cmd = (
        f"cd {shlex.quote(f'{service_dir}/infra')} && "
        f"docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml"
    )

    if action == DeployAction.STOP:
        cmd = f"{compose_cmd} stop"
    elif action == DeployAction.UNDEPLOY:
        # compose down first, rm -rf only if down succeeds.
        # If down fails — keep directory so retry can work.
        cmd = f"{compose_cmd} down -v && rm -rf {quoted_service_dir}"
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
            result = await conn.run(cmd, check=False)

            if result.exit_status != 0:
                error = f"SSH command failed (exit {result.exit_status}): {result.stderr}"
                logger.error(
                    "deploy_lifecycle_ssh_failed",
                    task_id=task_id,
                    action=action.value,
                    error=error,
                )
                return {
                    "status": "failed",
                    "error": error,
                    "deploy_outcome": DeployOutcome.GIVE_UP.value,
                }

            logger.info(
                "deploy_lifecycle_success",
                task_id=task_id,
                action=action.value,
                project_name=str(project_slug),
                server_ip=server_ip,
                output=result.stdout[:500] if result.stdout else "",
            )

            return {
                "status": "success",
                "action": action.value,
                "deploy_outcome": DeployOutcome.SUCCESS.value,
            }

    except Exception as e:
        logger.error(
            "deploy_lifecycle_exception",
            task_id=task_id,
            action=action.value,
            error=str(e),
            exc_info=True,
        )
        return {
            "status": "failed",
            "error": str(e),
            "deploy_outcome": DeployOutcome.GIVE_UP.value,
        }
