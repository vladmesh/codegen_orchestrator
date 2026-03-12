"""DevOps subgraph nodes.

Contains functional nodes for secret resolution, readiness check, and deployment.
"""

import asyncio
from datetime import UTC, datetime
import os
import secrets as secrets_module

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.project import ServiceStatus
from shared.crypto import decrypt_dict

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from ...schemas.api_types import AllocationInfo
from .dotenv_builder import build_dotenv, encode_dotenv
from .env_groups import resolve_with_groups
from .state import DevOpsState

logger = structlog.get_logger()


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

        # Get previously saved secrets from project config (decrypt at rest)
        config_secrets = project_spec.get("config", {}).get("secrets", {})
        config_secrets = decrypt_dict(config_secrets) if config_secrets else {}

        resolved = {}
        missing_user = []
        newly_generated = {}  # Track new infra secrets to save

        # Phase 1: Split infra vars into cached vs uncached
        uncached_infra: set[str] = set()
        for var, var_type in env_analysis.items():
            if var_type == "infra":
                if var in config_secrets:
                    resolved[var] = config_secrets[var]
                    logger.debug("secret_reused", var=var)
                else:
                    uncached_infra.add(var)

        # Phase 2: Resolve uncached infra via groups (coherent passwords)
        if uncached_infra:
            grouped, remaining = resolve_with_groups(uncached_infra, safe_project_id)
            resolved.update(grouped)
            newly_generated.update(grouped)
            logger.debug("secrets_grouped", count=len(grouped), vars=sorted(grouped))

            # Fallback for remaining vars not covered by any group
            for var in remaining:
                resolved[var] = self._generate_infra_secret(var, safe_project_id)
                newly_generated[var] = resolved[var]
                logger.debug("secret_generated", var=var)

        # Phase 3: Computed and user secrets (unchanged)
        for var, var_type in env_analysis.items():
            if var_type == "computed":
                resolved[var] = self._compute_secret(var, project_spec, state)

            elif var_type == "user":
                # Check if user provided it via:
                # 1. provided_secrets (passed directly)
                # 2. project_spec.config.secrets (saved to DB via save_project_secret)
                if var in provided_secrets:
                    resolved[var] = provided_secrets[var]
                elif var in config_secrets:
                    resolved[var] = config_secrets[var]
                else:
                    missing_user.append(var)

        # Phase 4: Inject PO-provided secrets not in .env.example
        # env_analysis only covers vars from .env.example. PO may have set
        # secrets (e.g. ADMIN_TELEGRAM_ID) that the worker used in code but
        # didn't add to .env.example. Ensure they always reach the .env.
        for var, val in config_secrets.items():
            if var not in resolved:
                resolved[var] = val
                logger.info("secret_injected_from_config", var=var)

        # Save newly generated secrets to project config for reuse on redeploy
        if newly_generated and project_id != "unknown":
            await self._save_secrets_to_project(project_id, newly_generated)

        logger.info(
            "secret_resolver_complete",
            resolved_count=len(resolved),
            missing_user_count=len(missing_user),
            missing_user=missing_user,
            newly_generated_count=len(newly_generated),
        )

        return {
            "resolved_secrets": resolved,
            "missing_user_secrets": missing_user,
        }

    def _find_allocation(self, state: DevOpsState, service_name: str) -> tuple[str, int] | None:
        """Look up allocated server IP and port for a service.

        Searches allocated_resources by matching service_name field.

        Returns:
            (server_ip, port) tuple or None if not found.
        """
        resources = state.get("allocated_resources", {})
        for alloc in resources.values():
            if isinstance(alloc, dict) and alloc.get("service_name") == service_name:
                ip = alloc.get("server_ip", "localhost")
                port = alloc.get("port", 8000)
                return ip, port
        return None

    def _generate_infra_secret(self, key: str, project_id: str) -> str:
        """Generate infrastructure secret value (fallback for vars not covered by groups)."""
        key_upper = key.upper()

        if "SECRET" in key_upper or ("KEY" in key_upper and "API" not in key_upper):
            return secrets_module.token_urlsafe(32)

        # Default random for unknown infra
        return secrets_module.token_urlsafe(32)

    def _compute_secret(self, key: str, project_spec: dict, state: DevOpsState) -> str:  # noqa: PLR0911
        """Compute context-based secret value."""
        key_upper = key.upper()

        if key_upper == "APP_NAME":
            return project_spec.get("name", "app").replace(" ", "_").lower()

        elif key_upper in {"APP_ENV", "ENVIRONMENT"}:
            return "production"

        elif key_upper == "DEBUG":
            return "false"

        elif key_upper == "PROJECT_NAME":
            return project_spec.get("name", "project")

        elif key_upper == "POSTGRES_HOST":
            return "db"  # Docker service name

        elif key_upper == "POSTGRES_PORT":
            return "5432"

        elif key_upper == "POSTGRES_REQUIRE_SSL":
            return "false"

        elif key_upper in {"BACKEND_PORT", "FRONTEND_PORT", "TG_BOT_PORT"}:
            # Map VAR_PORT -> service name in allocator
            service_map = {
                "BACKEND_PORT": "backend",
                "FRONTEND_PORT": "frontend",
                "TG_BOT_PORT": "tg_bot",
            }
            service = service_map.get(key_upper, "backend")
            alloc = self._find_allocation(state, service)
            if alloc:
                return str(alloc[1])
            return "8000"

        elif key_upper in {"BACKEND_API_URL", "API_URL", "API_BASE_URL", "BACKEND_URL"}:
            # Inter-service URL: use docker service name, not external IP
            return "http://backend:8000"

        # Docker images from self-hosted registry
        elif key_upper.endswith("_IMAGE"):
            registry_host = os.getenv("ORCHESTRATOR_HOSTNAME")
            if not registry_host:
                raise RuntimeError("ORCHESTRATOR_HOSTNAME is not set")
            repo_info = state.get("repo_info") or {}
            repo_url = repo_info.get("html_url", "")
            if repo_url:
                # Parse: https://github.com/org/repo -> org/repo
                parts = repo_url.rstrip("/").split("/")
                owner = parts[-2] if len(parts) > 1 else "unknown"
                repo = parts[-1] if parts else "unknown"

                # Derive service name: BACKEND_IMAGE -> backend, TG_BOT_IMAGE -> tg-bot
                service = key_upper.replace("_IMAGE", "").lower().replace("_", "-")

                return f"{registry_host}/{owner}/{repo}-{service}:latest"
            return f"{registry_host}/unknown/unknown-service:latest"

        # Default: project name
        return project_spec.get("name", "value")

    async def _save_secrets_to_project(self, project_id: str, secrets: dict) -> None:
        """Save newly generated secrets to project config for reuse on redeploy.

        Uses the atomic merge endpoint to avoid race conditions.

        Args:
            project_id: Project ID
            secrets: Dict of secret_name -> secret_value to save
        """
        try:
            await api_client.merge_secrets(project_id, secrets)

            logger.info(
                "secrets_saved_to_project",
                project_id=project_id,
                secrets_count=len(secrets),
                secret_names=list(secrets.keys()),
            )
        except Exception as e:
            # Log but don't fail - secrets will be regenerated next time
            logger.error(
                "save_secrets_failed",
                project_id=project_id,
                error=str(e),
                error_type=type(e).__name__,
            )


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
    deployed_sha: str | None = None,
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
    if deployed_sha:
        payload["deployed_sha"] = deployed_sha

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


async def _write_deploy_secrets(
    github_client: GitHubAppClient,
    owner: str,
    repo: str,
    server_ip: str,
    port: int,
    project_name: str,
    dotenv_b64: str,
    ssh_key: str,
) -> bool:
    """Write deployment secrets to GitHub repository for deploy.yml workflow."""
    # Registry credentials for CI docker push
    registry_url = os.getenv("ORCHESTRATOR_HOSTNAME")
    if not registry_url:
        logger.error("registry_env_missing", var="ORCHESTRATOR_HOSTNAME")
        return False
    registry_user = os.getenv("REGISTRY_USER")
    if not registry_user:
        logger.error("registry_env_missing", var="REGISTRY_USER")
        return False
    registry_password = os.getenv("REGISTRY_PASSWORD")
    if not registry_password:
        logger.error("registry_env_missing", var="REGISTRY_PASSWORD")
        return False

    secrets_map = {
        "DOTENV": dotenv_b64,
        "DEPLOY_HOST": server_ip,
        "DEPLOY_USER": "root",
        "DEPLOY_SSH_KEY": ssh_key,
        "DEPLOY_PORT": str(port),
        "PROJECT_NAME": project_name,
        "REGISTRY_URL": registry_url,
        "REGISTRY_USER": registry_user,
        "REGISTRY_PASSWORD": registry_password,
    }

    try:
        count = await github_client.set_repository_secrets(owner, repo, secrets_map)
        logger.info(
            "deploy_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=count,
            total=len(secrets_map),
        )
        return count == len(secrets_map)
    except Exception as e:
        logger.error(
            "deploy_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


class DeployerNode(FunctionalNode):
    """Deploy via GitHub Actions: write secrets, dispatch deploy.yml, wait for completion."""

    def __init__(self):
        super().__init__(node_id="deployer")

    async def _try_deploy_rerun(
        self,
        github: GitHubAppClient,
        owner: str,
        repo: str,
        dispatch_time: datetime,
    ) -> dict | None:
        """Attempt to rerun failed deploy workflow jobs.

        Returns run_info dict on success, None on failure or if rerun is not possible.
        """
        try:
            failed_run = await github.get_latest_workflow_run(
                owner, repo, "deploy.yml", "main", created_after=dispatch_time
            )
            if not failed_run:
                logger.warning("deploy_rerun_no_run_found")
                return None

            run_id = failed_run["id"]
            logger.info("deploy_rerun_attempting", run_id=run_id)

            await github.rerun_failed_jobs(owner, repo, run_id)
            await asyncio.sleep(3)

            run_info = await github.wait_for_run_completion(
                owner, repo, run_id, timeout_seconds=600
            )
            logger.info("deploy_rerun_passed", run_id=run_id)
            return run_info

        except (RuntimeError, TimeoutError) as e:
            logger.error("deploy_rerun_failed", error=str(e))
            return None
        except Exception as e:
            logger.error("deploy_rerun_api_error", error=str(e))
            return None

    def _extract_deploy_params(self, state: DevOpsState) -> dict | None:
        """Extract and validate deployment parameters from state. Returns None on error."""
        project_spec = state.get("project_spec") or {}
        allocated_resources = state.get("allocated_resources", {})

        repo_info = state.get("repo_info") or {}
        repo_url = repo_info.get("html_url", "")
        if not repo_url:
            return None

        parts = repo_url.rstrip("/").split("/")
        first_resource = next(iter(allocated_resources.values()), {}) if allocated_resources else {}

        return {
            "owner": parts[-2],
            "repo": parts[-1],
            "project_name": project_spec.get("name", "project").replace(" ", "_").lower(),
            "server_ip": first_resource.get("server_ip"),
            "port": first_resource.get("port"),
            "server_handle": first_resource.get("server_handle"),
        }

    async def run(self, state: DevOpsState) -> dict:
        """Build DOTENV, write GitHub secrets, trigger deploy.yml, wait for result."""
        project_id = state.get("project_id")
        project_spec = state.get("project_spec") or {}
        resolved_secrets = state.get("resolved_secrets", {})
        logger.info("deployer_start", project_id=project_id)

        if not project_id:
            return {
                "deployment_result": {"status": "failed", "error": "No project_id"},
                "errors": ["No project_id for deployment"],
            }

        params = self._extract_deploy_params(state)
        if not params:
            return {
                "deployment_result": {"status": "failed", "error": "No repository URL"},
                "errors": ["No repository URL found in project spec"],
            }

        owner, repo = params["owner"], params["repo"]
        project_name = params["project_name"]
        server_ip, port = params["server_ip"], params["port"]
        server_handle = params["server_handle"]

        if not server_ip or not port:
            return {
                "deployment_result": {"status": "failed", "error": "No allocated resources"},
                "errors": ["No server_ip/port in allocated_resources"],
            }

        try:
            github = GitHubAppClient()

            # 0. Fetch SSH key for target server from DB
            ssh_key = await api_client.get_server_ssh_key(server_handle) if server_handle else None
            if not ssh_key:
                logger.error("deploy_ssh_key_not_found", server_handle=server_handle)
                return {
                    "deployment_result": {
                        "status": "failed",
                        "error": f"No SSH key in DB for server {server_handle}",
                    },
                    "errors": [f"No SSH key for server {server_handle}"],
                }

            # 1. Build and encode DOTENV
            dotenv_content = build_dotenv(resolved_secrets)
            dotenv_b64 = encode_dotenv(dotenv_content)

            # 2. Write deploy secrets to GitHub
            logger.info(
                "deploy_secrets_preview",
                server_ip=server_ip,
                port=port,
                project_name=project_name,
                owner=owner,
                repo=repo,
                dotenv_len=len(dotenv_b64),
            )
            secrets_ok = await _write_deploy_secrets(
                github_client=github,
                owner=owner,
                repo=repo,
                server_ip=server_ip,
                port=port,
                project_name=project_name,
                dotenv_b64=dotenv_b64,
                ssh_key=ssh_key,
            )

            if not secrets_ok:
                logger.error(
                    "deploy_secrets_write_failed",
                    server_ip=server_ip,
                    owner=owner,
                    repo=repo,
                )

            # 3. Record dispatch time BEFORE triggering (for race condition safety)
            dispatch_time = datetime.now(UTC)

            # 4. Trigger deploy workflow
            await github.trigger_workflow_dispatch(owner, repo, "deploy.yml")

            # 5. Wait for workflow completion
            run_info = await github.wait_for_workflow_completion(
                owner=owner,
                repo=repo,
                workflow_file="deploy.yml",
                branch="main",
                timeout_seconds=600,
                created_after=dispatch_time,
            )

            logger.info(
                "deploy_completed",
                owner=owner,
                repo=repo,
                run_id=run_info["id"],
                head_sha=run_info.get("head_sha"),
            )

            # 6. Create service deployment record
            allocations: list[AllocationInfo] = await api_client.get_project_allocations(project_id)
            target_alloc = allocations[0] if allocations else None

            config = project_spec.get("config") or {}
            modules = config.get("modules", "backend")
            if isinstance(modules, list):
                modules = ",".join(modules)

            if target_alloc:
                await _create_service_deployment_record(
                    project_id=project_id,
                    service_name=project_name,
                    server_handle=target_alloc.get("server_handle"),
                    port=port,
                    deployment_info={
                        "repo_full_name": f"{owner}/{repo}",
                        "branch": "main",
                        "modules": modules,
                    },
                    deployed_sha=run_info.get("head_sha"),
                )

            # 7. Update project status to active
            await api_client.patch(
                f"/projects/{project_id}",
                json={"service_status": ServiceStatus.RUNNING.value},
            )

            deployed_url = f"http://{server_ip}:{port}"
            return {
                "deployment_result": {"status": "success", "run_id": run_info["id"]},
                "deployed_url": deployed_url,
                "messages": [AIMessage(content=f"Deployment successful! URL: {deployed_url}")],
            }

        except (RuntimeError, TimeoutError) as e:
            logger.warning("deploy_workflow_failed", error=str(e))

            # Attempt to rerun failed jobs (gets a new GH Actions runner)
            run_info = await self._try_deploy_rerun(github, owner, repo, dispatch_time)
            if run_info:
                logger.info(
                    "deploy_completed",
                    owner=owner,
                    repo=repo,
                    run_id=run_info["id"],
                    head_sha=run_info.get("head_sha"),
                    rerun=True,
                )

                allocations: list[AllocationInfo] = await api_client.get_project_allocations(
                    project_id
                )
                target_alloc = allocations[0] if allocations else None

                config = project_spec.get("config") or {}
                modules = config.get("modules", "backend")
                if isinstance(modules, list):
                    modules = ",".join(modules)

                if target_alloc:
                    await _create_service_deployment_record(
                        project_id=project_id,
                        service_name=project_name,
                        server_handle=target_alloc.get("server_handle"),
                        port=port,
                        deployment_info={
                            "repo_full_name": f"{owner}/{repo}",
                            "branch": "main",
                            "modules": modules,
                        },
                        deployed_sha=run_info.get("head_sha"),
                    )

                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"service_status": ServiceStatus.RUNNING.value},
                )

                deployed_url = f"http://{server_ip}:{port}"
                return {
                    "deployment_result": {
                        "status": "success",
                        "run_id": run_info["id"],
                    },
                    "deployed_url": deployed_url,
                    "messages": [
                        AIMessage(
                            content=f"Deployment successful (after rerun)! URL: {deployed_url}"
                        )
                    ],
                }

            # Rerun failed or not possible — mark project as error
            try:
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"service_status": ServiceStatus.DOWN.value},
                )
            except Exception as status_err:
                logger.warning("status_update_failed", error=str(status_err))

            error_prefix = (
                "Deploy timeout" if isinstance(e, TimeoutError) else "Deploy workflow failed"
            )
            return {
                "deployment_result": {"status": "failed", "error": str(e)},
                "errors": [f"{error_prefix}: {e}"],
            }

        except Exception as e:
            logger.error("deployer_failed", error=str(e), exc_info=True)
            try:
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"service_status": ServiceStatus.DOWN.value},
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
