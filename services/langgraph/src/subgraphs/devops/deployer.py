"""DeployerNode — deploy via GitHub Actions: write secrets, dispatch deploy.yml, wait."""

import asyncio
from datetime import UTC, datetime
import os

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.application import ApplicationStatus

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from .dotenv_builder import build_dotenv, encode_dotenv
from .state import DevOpsState

logger = structlog.get_logger()


async def _create_deployment_record(
    project_id: str,
    service_name: str,
    server_handle: str,
    port: int,
    deployment_info: dict,
    deployed_sha: str | None = None,
) -> int | None:
    """Create a deployment record and update the Application status via API.

    Application should already exist (created during resource allocation).

    Returns:
        application_id if successfully resolved, None otherwise.
    """
    try:
        # Find existing Application (created during allocation)
        application_id = None
        repo = await api_client.get_primary_repository(project_id)
        if repo:
            app = await api_client.get_or_create_application(
                repo_id=repo.id,
                server_handle=server_handle,
                service_name=service_name,
            )
            application_id = app.get("id")

            # Update Application status to running
            await api_client.update_application(
                application_id, {"status": ApplicationStatus.RUNNING.value}
            )

        # Create Deployment record
        payload = {
            "project_id": project_id,
            "service_name": service_name,
            "server_handle": server_handle,
            "port": port,
            "result": "success",
            "deployment_info": deployment_info,
        }
        if application_id:
            payload["application_id"] = application_id
        if deployed_sha:
            payload["deployed_sha"] = deployed_sha

        await api_client.create_deployment(payload)
        logger.info("deployment_record_created", service_name=service_name)
        return application_id
    except Exception as e:
        logger.error(
            "deployment_record_error",
            service_name=service_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


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

            # 1. Build and encode DOTENV (include project_id for Promtail label discovery)
            all_env = {**resolved_secrets, "CODEGEN_PROJECT_ID": project_id}
            dotenv_content = build_dotenv(all_env)
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
            config = project_spec.get("config") or {}
            modules = config.get("modules", "backend")
            if isinstance(modules, list):
                modules = ",".join(modules)

            application_id = await _create_deployment_record(
                project_id=project_id,
                service_name=project_name,
                server_handle=server_handle,
                port=port,
                deployment_info={
                    "repo_full_name": f"{owner}/{repo}",
                    "branch": "main",
                    "modules": modules,
                },
                deployed_sha=run_info.get("head_sha"),
            )

            deployed_url = f"http://{server_ip}:{port}"
            return {
                "deployment_result": {"status": "success", "run_id": run_info["id"]},
                "deployed_url": deployed_url,
                "application_id": application_id,
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

                config = project_spec.get("config") or {}
                modules = config.get("modules", "backend")
                if isinstance(modules, list):
                    modules = ",".join(modules)

                application_id = await _create_deployment_record(
                    project_id=project_id,
                    service_name=project_name,
                    server_handle=server_handle,
                    port=port,
                    deployment_info={
                        "repo_full_name": f"{owner}/{repo}",
                        "branch": "main",
                        "modules": modules,
                    },
                    deployed_sha=run_info.get("head_sha"),
                )

                deployed_url = f"http://{server_ip}:{port}"
                return {
                    "deployment_result": {
                        "status": "success",
                        "run_id": run_info["id"],
                    },
                    "deployed_url": deployed_url,
                    "application_id": application_id,
                    "messages": [
                        AIMessage(
                            content=f"Deployment successful (after rerun)! URL: {deployed_url}"
                        )
                    ],
                }

            error_prefix = (
                "Deploy timeout" if isinstance(e, TimeoutError) else "Deploy workflow failed"
            )
            return {
                "deployment_result": {"status": "failed", "error": str(e)},
                "errors": [f"{error_prefix}: {e}"],
            }

        except Exception as e:
            logger.error("deployer_failed", error=str(e), exc_info=True)
            return {
                "deployment_result": {"status": "error", "error": str(e)},
                "errors": [f"Deployment error: {e}"],
            }
