"""DevOps subgraph nodes.

Contains functional nodes for secret resolution, readiness check, and deployment.
"""

import os
import secrets as secrets_module

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient

from ...clients.api import api_client
from ...config.constants import Paths
from ...nodes.base import FunctionalNode
from ...schemas.api_types import AllocationInfo, get_repo_url
from ...tools.devops_delegation import delegate_ansible_deploy
from .state import DevOpsState

logger = structlog.get_logger()

# SSH key path for CI secrets
SSH_KEY_PATH = Paths.SSH_KEY


class SecretResolverNode(FunctionalNode):
    """Resolve secrets by generating infra, computing context-based, and checking user-provided."""

    def __init__(self):
        super().__init__(node_id="secret_resolver")

    async def run(self, state: DevOpsState) -> dict:
        """Resolve all secrets based on classification."""
        logger.info("secret_resolver_start")

        env_analysis = state.get("env_analysis", {})
        provided_secrets = state.get("provided_secrets", {})
        project_spec = state.get("project_spec") or {}
        project_id = state.get("project_id") or "unknown"

        # Normalize project_id for DB names
        safe_project_id = project_id.replace("-", "_").lower()

        resolved = {}
        missing_user = []

        for var, var_type in env_analysis.items():
            if var_type == "infra":
                resolved[var] = self._generate_infra_secret(var, safe_project_id)

            elif var_type == "computed":
                resolved[var] = self._compute_secret(var, project_spec, state)

            elif var_type == "user":
                # Check if user provided it via:
                # 1. provided_secrets (passed directly)
                # 2. project_spec.config.secrets (saved to DB via save_project_secret)
                config_secrets = project_spec.get("config", {}).get("secrets", {})
                if var in provided_secrets:
                    resolved[var] = provided_secrets[var]
                elif var in config_secrets:
                    resolved[var] = config_secrets[var]
                else:
                    missing_user.append(var)

        logger.info(
            "secret_resolver_complete",
            resolved_count=len(resolved),
            missing_user_count=len(missing_user),
            missing_user=missing_user,
        )

        return {
            "resolved_secrets": resolved,
            "missing_user_secrets": missing_user,
        }

    def _generate_infra_secret(self, key: str, project_id: str) -> str:
        """Generate infrastructure secret value."""
        key_upper = key.upper()

        if key_upper == "REDIS_URL":
            return "redis://redis:6379/0"

        elif key_upper == "DATABASE_URL":
            db_pass = secrets_module.token_urlsafe(16)
            db_name = f"db_{project_id}"
            return f"postgresql://postgres:{db_pass}@postgres:5432/{db_name}"

        elif "SECRET" in key_upper or ("KEY" in key_upper and "API" not in key_upper):
            return secrets_module.token_urlsafe(32)

        elif key_upper == "POSTGRES_USER":
            return "postgres"

        elif key_upper == "POSTGRES_PASSWORD":
            return secrets_module.token_urlsafe(16)

        elif key_upper == "POSTGRES_DB":
            return f"db_{project_id}"

        # Default random for unknown infra
        return secrets_module.token_urlsafe(32)

    def _compute_secret(self, key: str, project_spec: dict, state: DevOpsState) -> str:
        """Compute context-based secret value."""
        key_upper = key.upper()

        if key_upper == "APP_NAME":
            return project_spec.get("name", "app").replace(" ", "_").lower()

        elif key_upper in {"APP_ENV", "ENVIRONMENT"}:
            return "production"

        elif key_upper == "PROJECT_NAME":
            return project_spec.get("name", "project")

        elif key_upper in {"BACKEND_API_URL", "API_URL"}:
            resources = state.get("allocated_resources", {})
            if resources:
                first_resource = list(resources.values())[0]
                if isinstance(first_resource, dict):
                    ip = first_resource.get("server_ip", "localhost")
                    port = first_resource.get("port", 8000)
                    return f"http://{ip}:{port}"
            return "http://localhost:8000"

        # Default: project name
        return project_spec.get("name", "value")


class ReadinessCheckNode(FunctionalNode):
    """Check if all user secrets are provided."""

    def __init__(self):
        super().__init__(node_id="readiness_check")

    async def run(self, state: DevOpsState) -> dict:
        """Check deployment readiness."""
        missing = state.get("missing_user_secrets", [])

        if missing:
            logger.info(
                "readiness_check_missing_secrets",
                missing=missing,
            )
            return {
                "messages": [
                    AIMessage(
                        content=f"Missing user secrets: {', '.join(missing)}. "
                        "Please provide these secrets to continue deployment."
                    )
                ],
            }

        logger.info("readiness_check_ready")
        return {}


async def _create_service_deployment_record(
    project_id: str,
    service_name: str,
    server_handle: str,
    port: int,
    deployment_info: dict,
) -> bool:
    """Create a service deployment record via API."""
    payload = {
        "project_id": project_id,
        "service_name": service_name,
        "server_handle": server_handle,
        "port": port,
        "status": "running",
        "deployment_info": deployment_info,
    }

    try:
        await api_client.create_service_deployment(payload)
        logger.info("service_deployment_record_created", service_name=service_name)
        return True
    except Exception as e:
        logger.error(
            "service_deployment_record_error",
            service_name=service_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


async def _setup_ci_secrets(
    github_client: GitHubAppClient,
    owner: str,
    repo: str,
    server_ip: str,
    project_name: str,
) -> bool:
    """Configure GitHub Actions secrets for CI/CD deployment."""
    # Read SSH private key from mounted volume
    if not os.path.exists(SSH_KEY_PATH):
        logger.warning("ssh_key_not_found", path=SSH_KEY_PATH)
        return False

    try:
        with open(SSH_KEY_PATH) as f:
            ssh_key = f.read()
    except Exception as e:
        logger.error("ssh_key_read_failed", error=str(e))
        return False

    secrets_map = {
        "DEPLOY_HOST": server_ip,
        "DEPLOY_USER": "root",
        "DEPLOY_SSH_KEY": ssh_key,
        "DEPLOY_PROJECT_PATH": f"/opt/services/{project_name}",
        "DEPLOY_COMPOSE_FILES": "infra/compose.base.yml infra/compose.prod.yml",
    }

    try:
        count = await github_client.set_repository_secrets(owner, repo, secrets_map)
        logger.info(
            "ci_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=count,
            total=len(secrets_map),
        )
        return count == len(secrets_map)
    except Exception as e:
        logger.error(
            "ci_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


class DeployerNode(FunctionalNode):
    """Execute Ansible deployment via delegation to infrastructure-worker."""

    def __init__(self):
        super().__init__(node_id="deployer")

    async def run(self, state: DevOpsState) -> dict:
        """Execute deployment by delegating to infrastructure-worker."""
        project_id = state.get("project_id")
        logger.info("deployer_start", project_id=project_id)

        resolved_secrets = state.get("resolved_secrets", {})
        project_spec = state.get("project_spec") or {}

        if not project_id:
            return {
                "deployment_result": {"status": "failed", "error": "No project_id"},
                "errors": ["No project_id for deployment"],
            }

        try:
            # Delegate deployment to infrastructure-worker
            result = await delegate_ansible_deploy.ainvoke(
                {
                    "project_id": project_id,
                    "secrets": resolved_secrets,
                }
            )

            logger.info(
                "deployer_complete",
                project_id=project_id,
                status=result.get("status"),
                deployed_url=result.get("deployed_url"),
            )

            if result.get("status") == "success":
                # Post-deployment operations (kept in langgraph)
                deployed_url = result.get("deployed_url")
                server_ip = result.get("server_ip")
                port = result.get("port")

                # Get allocation and repo info for post-deployment
                allocations: list[AllocationInfo] = await api_client.get_project_allocations(
                    project_id
                )
                target_alloc = allocations[0] if allocations else None

                repo_url = get_repo_url(project_spec)
                if repo_url:
                    parts = repo_url.rstrip("/").split("/")
                    repo = parts[-1] if len(parts) > 0 else "repo"
                    owner = parts[-2] if len(parts) > 1 else "owner"
                    repo_full_name = f"{owner}/{repo}"
                else:
                    repo_full_name = "unknown/unknown"
                    owner = "unknown"
                    repo = "unknown"

                project_name = project_spec.get("name", "project").replace(" ", "_").lower()
                config = project_spec.get("config") or {}
                modules = config.get("modules", "backend")
                if isinstance(modules, list):
                    modules = ",".join(modules)

                # Create service deployment record
                if target_alloc:
                    await _create_service_deployment_record(
                        project_id=project_id,
                        service_name=project_name,
                        server_handle=target_alloc.get("server_handle"),
                        port=port,
                        deployment_info={
                            "repo_full_name": repo_full_name,
                            "branch": "main",
                            "project_dir": f"/opt/services/{project_name}",
                            "compose_files": "infra/compose.base.yml infra/compose.prod.yml",
                            "modules": modules,
                        },
                    )

                # Setup CI secrets
                github_client = GitHubAppClient()
                await _setup_ci_secrets(
                    github_client=github_client,
                    owner=owner,
                    repo=repo,
                    server_ip=server_ip,
                    project_name=project_name,
                )

                # Update project status
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"status": "active"},
                )

                return {
                    "deployment_result": result,
                    "deployed_url": deployed_url,
                    "messages": [AIMessage(content=f"Deployment successful! URL: {deployed_url}")],
                }

            # Deployment failed
            await api_client.patch(
                f"/projects/{project_id}",
                json={"status": "error"},
            )
            return {
                "deployment_result": result,
                "errors": [result.get("error", "Deployment failed")],
                "messages": [AIMessage(content=f"Deployment failed: {result.get('error')}")],
            }

        except Exception as e:
            logger.error("deployer_failed", error=str(e), exc_info=True)
            try:
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"status": "error"},
                )
            except Exception as status_err:
                logger.warning("status_update_failed", error=str(status_err))
            return {
                "deployment_result": {"status": "error", "error": str(e)},
                "errors": [f"Deployment error: {e}"],
            }


# Node instances
secret_resolver_node = SecretResolverNode()
readiness_check_node = ReadinessCheckNode()
deployer_node = DeployerNode()
