"""Service recovery for provisioner - redeploys services after server recovery."""

import structlog

from shared.diagnostics import redact_diagnostic
from shared.notifications import notify_admins_best_effort

from ..clients.api import DeploymentRecord
from ..config.constants import Timeouts
from .ansible_runner import AnsibleRunner
from .api_client import get_services_on_server

logger = structlog.get_logger()

MAX_ERROR_PREVIEW = 5
MAX_ERROR_DETAIL = 500


async def redeploy_service(
    service: DeploymentRecord,
    server_ip: str,
    github_token: str,
) -> tuple[bool, str]:
    """Redeploy a single service to the recovered server.

    Args:
        service: Service deployment record from API
        server_ip: Server IP address
        github_token: GitHub token for repo access

    Returns:
        Tuple of (success: bool, message: str)
    """
    service_name = service.service_name
    repo_full_name = service.deployment_info.get("repo_full_name")
    port = service.port

    if not repo_full_name:
        return False, f"Service {service_name} has no repo_full_name in deployment_info"

    if not port:
        return False, f"Service {service_name} has no port"

    logger.info(
        "service_redeployment",
        service_name=service_name,
        server_ip=server_ip,
        port=port,
        status="start",
    )
    try:
        success, output = AnsibleRunner().run_playbook(
            server_ip=server_ip,
            server_handle=service.server_handle,
            playbook_name="deploy_project.yml",
            timeout=Timeouts.SERVICE_DEPLOY,
            extra_vars={
                "project_name": service_name,
                "repo_full_name": repo_full_name,
                "github_token": github_token,
                "service_port": str(port),
            },
            secret_values=(github_token,),
        )
        output = redact_diagnostic(output, secrets=(github_token,))
        if success:
            logger.info(
                "service_redeployment",
                service_name=service_name,
                server_ip=server_ip,
                port=port,
                status="success",
            )
            return True, f"Service {service_name} redeployed successfully"
        is_timeout = output.startswith("Timeout after ")
        # Keep stderr's reason as well as the closing stdout when a play is noisy.
        detail = output
        if len(detail) > MAX_ERROR_DETAIL:
            half = MAX_ERROR_DETAIL // 2
            detail = f"{output[:half]}\n...\n{output[-half:]}"
        logger.error(
            "service_redeployment",
            service_name=service_name,
            server_ip=server_ip,
            port=port,
            status="timeout" if is_timeout else "failed",
            error=detail,
        )
        if is_timeout:
            return False, f"Deployment timeout for {service_name}"
        return False, f"Ansible failed for {service_name}: {detail}"
    except Exception as exc:
        reason = redact_diagnostic(exc, secrets=(github_token,))[-500:]
        logger.error(
            "service_redeployment",
            service_name=service_name,
            server_ip=server_ip,
            port=port,
            status="error",
            error=reason,
            error_type=type(exc).__name__,
        )
        return False, f"Deployment error for {service_name}: {reason}"


async def redeploy_all_services(
    server_handle: str,
    server_ip: str,
) -> tuple[int, int, list[str]]:
    """Redeploy all services on a recovered server.

    Args:
        server_handle: Server handle
        server_ip: Server IP address

    Returns:
        Tuple of (success_count, fail_count, error_messages)
    """
    from shared.clients.github import GitHubAppClient

    logger.info(
        "incident_recovery_start",
        server_handle=server_handle,
        server_ip=server_ip,
    )
    services = await get_services_on_server(server_handle)

    if not services:
        logger.info("no_services_to_redeploy", server_handle=server_handle)
        logger.info(
            "incident_recovery_complete",
            server_handle=server_handle,
            server_ip=server_ip,
            success_count=0,
            fail_count=0,
        )
        return 0, 0, []

    logger.info("services_found_for_redeployment", server_handle=server_handle, count=len(services))

    # Get GitHub token
    github_client = GitHubAppClient()
    success_count = 0
    fail_count = 0
    errors = []

    for service in services:
        service_name = service.service_name
        repo_full_name = service.deployment_info.get("repo_full_name")

        if not repo_full_name:
            errors.append(f"{service_name}: no repo info")
            fail_count += 1
            continue

        try:
            owner, repo = repo_full_name.split("/")
            token = await github_client.get_token(owner, repo)
        except Exception as e:
            errors.append(f"{service_name}: failed to get token - {redact_diagnostic(e)[-500:]}")
            fail_count += 1
            continue

        success, message = await redeploy_service(service, server_ip, token)

        message = redact_diagnostic(message, secrets=(token,))
        if success:
            success_count += 1
            logger.info("service_redeployed", service_name=service_name)
        else:
            fail_count += 1
            errors.append(f"{service_name}: {message}")
            logger.error("service_redeploy_failed", service_name=service_name, error=message)

    # Notify about results
    if fail_count == 0 and success_count > 0:
        await notify_admins_best_effort(
            f"✅ All {success_count} services redeployed on *{server_handle}*",
            level="success",
            server_handle=server_handle,
        )
    elif fail_count > 0:
        error_summary = "\n".join(errors[:MAX_ERROR_PREVIEW])
        if len(errors) > MAX_ERROR_PREVIEW:
            error_summary += f"\n...and {len(errors) - MAX_ERROR_PREVIEW} more"

        await notify_admins_best_effort(
            f"⚠️ Service redeployment on *{server_handle}*:\n"
            f"✅ {success_count} succeeded\n"
            f"❌ {fail_count} failed\n\n"
            f"Errors:\n{error_summary}",
            level="warning",
            server_handle=server_handle,
        )

    logger.info(
        "incident_recovery_complete",
        server_handle=server_handle,
        server_ip=server_ip,
        success_count=success_count,
        fail_count=fail_count,
    )
    return success_count, fail_count, errors
