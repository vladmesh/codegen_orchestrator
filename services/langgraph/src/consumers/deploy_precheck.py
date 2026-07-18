"""Deploy pre-check: validate server state via SSH before deploying."""

from __future__ import annotations

import shlex

import asyncssh
import structlog

from shared.contracts.dto.project import ProjectDTO
from shared.contracts.runtime_project import runtime_project_slug

from ..clients.api import api_client

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"


async def _pre_check_server(
    server_ip: str,
    ssh_user: str,
    ssh_key: str,
    project_name: str,
    action: str,
) -> str | None:
    """Validate server state before deploy via SSH.

    Checks /opt/services/<project_name>/ directory:
    - create: directory must NOT exist (fail if leftover from previous run)
    - feature/fix: directory MUST exist (project must be deployed already)

    Returns:
        Error message string if pre-check failed, None if OK.
    """
    try:
        project_slug = runtime_project_slug(project_name)
    except ValueError as e:
        return str(e)
    service_dir = f"{SERVICE_BASE_DIR}/{project_slug}/"
    quoted_service_dir = shlex.quote(service_dir)

    try:
        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username=ssh_user,
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            result = await conn.run(f"test -d {quoted_service_dir}", check=False)
            dir_exists = result.exit_status == 0

    except Exception as e:
        logger.warning(
            "deploy_precheck_ssh_failed",
            server_ip=server_ip,
            error=str(e),
        )
        return f"SSH pre-check failed for {server_ip}: {e}"

    if action == "create" and dir_exists:
        return (
            f"Service dir {service_dir} already exists on {server_ip}. "
            "Clean up the previous deployment or use action='feature'."
        )

    if action in ("feature", "fix") and not dir_exists:
        return (
            f"Service dir {service_dir} not found on {server_ip}. "
            "Project was never deployed. Use action='create' for first deploy."
        )

    logger.info(
        "deploy_precheck_ok",
        server_ip=server_ip,
        project_name=str(project_slug),
        action=action,
        dir_exists=dir_exists,
    )
    return None


async def _run_deploy_precheck(
    allocated_resources: dict, project: ProjectDTO, project_id: str, action: str
) -> str | None:
    """Run SSH pre-check against the target server. Returns error or None."""
    first_resource = next(iter(allocated_resources.values()), {})
    if not isinstance(first_resource, dict):
        return None
    server_ip = first_resource.get("server_ip")
    server_handle = first_resource.get("server_handle")
    if not server_ip or not server_handle:
        return None

    try:
        project_name = runtime_project_slug(project.name or project_id)
    except ValueError as e:
        return str(e)
    server = await api_client.get_server(server_handle)
    ssh_key = await api_client.get_server_ssh_key(server_handle)
    if not ssh_key:
        return None

    return await _pre_check_server(
        server_ip=server_ip,
        ssh_user=server.ssh_user,
        ssh_key=ssh_key,
        project_name=str(project_name),
        action=action,
    )
