"""DeployerNode — deploy via GitHub Actions: write secrets, dispatch deploy.yml, wait."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
import os

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient, deploy_pin_tag
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deploy_dispatch import DeployDispatchClaim
from shared.contracts.env_overrides import env_overrides_digest
from shared.contracts.service_ports import is_http_health_port_service
from shared.diagnostics import redact_diagnostic

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from ...runtime_identity import project_spec_runtime_slug
from .dotenv_builder import build_dotenv, encode_dotenv
from .state import DevOpsState

logger = structlog.get_logger()

DEPLOY_WORKFLOW = "deploy.yml"
DEPLOY_TIMEOUT_SECONDS = 600

# Cancellation is signalled by the GitHub client through exception types that this
# module must not import (tests substitute their own doubles), so they are matched
# by name.
_CANCELLATION_ERRORS = ("WorkflowCancelledError", "WorkflowCancellationUnprovenError")


def _resolved_secret_values(values: object) -> tuple[str, ...]:
    """Return state secrets that diagnostics must never cross a boundary with."""
    if not isinstance(values, dict):
        return ()
    return tuple(value for value in values.values() if isinstance(value, str) and value)


class DeployRefusedError(RuntimeError):
    """This deploy cannot be called successful. Refuses the deploy, does not stop the service."""


class DeployedShaMismatchError(DeployRefusedError):
    """The finished deploy run is not the commit the deploy asked for."""


class DeployPinTagLeakedError(DeployRefusedError):
    """The temporary pin tag survived the run, so the deploy left litter in the user's repo."""


class DeployFenceUnprovenError(DeployRefusedError):
    """An older deploy run may still be able to write, so this one cannot be the last word."""


class DeployDispatchWithdrawnError(RuntimeError):
    """This run was stopped before it reached GitHub, so nothing was dispatched.

    Not a DeployRefusedError: nothing failed. The deploy was called off while it
    could still be called off, which is the whole point of asking.
    """


def _require_live_lease(claim: DeployDispatchClaim | None, moment: datetime) -> None:
    """Refuse to dispatch once the claim's deadline has passed.

    Holding the boundary is a lease, not a possession, and this is the promise
    that makes it one: reconciliation may take a claim back after its deadline,
    and a worker that stalled in between should not go on to start work nobody is
    waiting for.

    This is housekeeping, not a fence. Read on the worker's own clock and one
    HTTP call before the effect, it cannot rule out a dispatch GitHub accepts
    after the deadline anyway, so nothing may be recorded as removed on the
    strength of it. What a caller removing a value relies on instead is reading
    the deployed service back and repeating itself until what it reads is what
    it asked for.
    """
    if claim is None or claim.lease_expires_at is None:
        return
    if moment < claim.lease_expires_at:
        return
    logger.warning(
        "deploy_dispatch_lease_expired",
        run_id=claim.run_id,
        lease_expires_at=claim.lease_expires_at.isoformat(),
    )
    raise DeployDispatchWithdrawnError(
        f"deploy run {claim.run_id} held its dispatch claim past "
        f"{claim.lease_expires_at.isoformat()} and may no longer dispatch"
    )


async def _create_deployment_record(
    project_id: str,
    service_name: str,
    server_handle: str,
    port: int,
    deployment_info: dict,
    deployed_sha: str | None = None,
    diagnostic_secrets: Iterable[str] = (),
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
            error=redact_diagnostic(e, secrets=diagnostic_secrets),
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
    ssh_user: str,
    diagnostic_secrets: Iterable[str] = (),
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
        "DEPLOY_USER": ssh_user,
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
            error=redact_diagnostic(e, secrets=diagnostic_secrets),
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
        ref: str = "main",
        head_sha: str | None = None,
        deploy_run_id: str | None = None,
        diagnostic_secrets: Iterable[str] = (),
    ) -> dict | None:
        """Attempt to rerun failed deploy workflow jobs.

        Returns run_info dict on success, None on failure or if rerun is not possible.
        A cancellation during the rerun is re-raised rather than downgraded to None:
        the rerun is live work that teardown has to stop before cleanup runs.
        """
        try:
            failed_run = await github.get_latest_workflow_run(
                owner,
                repo,
                DEPLOY_WORKFLOW,
                ref,
                created_after=dispatch_time,
                head_sha=head_sha,
            )
            if not failed_run:
                logger.warning("deploy_rerun_no_run_found")
                return None

            run_id = failed_run["id"]
            logger.info("deploy_rerun_attempting", run_id=run_id)

            # A rerun restarts the same external effect, so it crosses the same
            # boundary and asks the same question first.
            claim = await self._claim_dispatch(deploy_run_id)
            _require_live_lease(claim, datetime.now(UTC))
            await github.rerun_failed_jobs(owner, repo, run_id)
            await asyncio.sleep(3)

            run_info = await github.wait_for_run_completion(
                owner,
                repo,
                run_id,
                timeout_seconds=DEPLOY_TIMEOUT_SECONDS,
                cancel_check=lambda: self._run_cancelled(deploy_run_id),
            )
            logger.info("deploy_rerun_passed", run_id=run_id)
            return run_info

        except DeployDispatchWithdrawnError:
            # The run was called off before the rerun was requested. Swallowing
            # it here would report "rerun not possible" and let the caller retry
            # an effect somebody already withdrew.
            raise
        except (RuntimeError, TimeoutError) as e:
            if type(e).__name__ in _CANCELLATION_ERRORS:
                raise
            logger.error(
                "deploy_rerun_failed", error=redact_diagnostic(e, secrets=diagnostic_secrets)
            )
            return None
        except Exception as e:
            logger.error(
                "deploy_rerun_api_error", error=redact_diagnostic(e, secrets=diagnostic_secrets)
            )
            return None

    async def _remove_pin_tag(
        self,
        github: GitHubAppClient,
        owner: str,
        repo: str,
        tag: str,
        diagnostic_secrets: Iterable[str] = (),
    ) -> Exception | None:
        """Drop the pin tag whatever the run did. A leftover tag is litter in the user's repo.

        Returns the failure instead of raising, so cleanup never masks the exception
        the run itself is already propagating. The caller turns a surviving tag into
        a refused deploy.
        """
        try:
            await asyncio.shield(github.delete_ref(owner, repo, f"tags/{tag}"))
        except Exception as e:
            logger.error(
                "deploy_pin_tag_cleanup_failed",
                owner=owner,
                repo=repo,
                tag=tag,
                error=redact_diagnostic(e, secrets=diagnostic_secrets),
                error_type=type(e).__name__,
            )
            return e
        return None

    async def _fence_active_deploys(
        self, github: GitHubAppClient, owner: str, repo: str, project_id: str
    ) -> list[int]:
        """Stop every deploy run that could still write the payload this one replaced.

        The project deploy lock only serialises consumers, and it expires; the
        GitHub Actions run it started does not stop when it does. So a deploy that
        exists to remove a value stops the runs that can still write the old one:
        called after the new payload is in the repository secrets, it covers the
        runs that already read the old payload, while the write itself covers
        everything created afterwards.

        Together they make the window small, not empty. GitHub is asynchronous
        and not ours, so a dispatch already in flight can still be accepted after
        all of this. Whoever needs the old value gone confirms that by reading
        the deployed service; this only shortens the wait. An unproven stop
        refuses the deploy rather than reporting a removal it cannot claim.
        """
        try:
            fenced = await github.fence_workflow(owner, repo, DEPLOY_WORKFLOW)
        except Exception as e:
            if type(e).__name__ not in _CANCELLATION_ERRORS:
                raise
            raise DeployFenceUnprovenError(
                f"an earlier {DEPLOY_WORKFLOW} run in {owner}/{repo} could not be proven "
                f"stopped: {e}"
            ) from e
        if fenced:
            logger.info(
                "deploy_fenced_active_runs",
                project_id=project_id,
                owner=owner,
                repo=repo,
                run_ids=fenced,
            )
        return fenced

    async def _dispatch_and_wait(
        self,
        github: GitHubAppClient,
        owner: str,
        repo: str,
        head_sha: str,
        run_id: str | None,
        diagnostic_secrets: Iterable[str] = (),
    ) -> tuple[dict, bool]:
        """Run deploy.yml, optionally pinned to one commit. Returns (run_info, was_rerun).

        workflow_dispatch only accepts a branch or a tag in ``ref`` (a bare SHA is
        rejected with 422), so a requested commit is pinned by a temporary tag that
        is dropped on every outcome. Without ``head_sha`` this deploys whatever is on
        main, as before.
        """
        pin_tag = deploy_pin_tag(head_sha) if head_sha else None
        ref = pin_tag or "main"
        rerun = False
        cleanup_error: Exception | None = None
        try:
            # Inside the cleanup guard: an interrupted create can still have reached
            # GitHub, and a tag applied but not tracked is exactly the litter case.
            if pin_tag:
                await github.create_or_reset_tag(owner, repo, pin_tag, head_sha)

            # Last thing before the deploy leaves the system. After this the run
            # exists on GitHub Actions and can only be stopped there.
            claim = await self._claim_dispatch(run_id)

            # Record dispatch time BEFORE triggering (for race condition safety)
            dispatch_time = datetime.now(UTC)
            _require_live_lease(claim, dispatch_time)
            if pin_tag:
                await github.trigger_workflow_dispatch(owner, repo, DEPLOY_WORKFLOW, ref=pin_tag)
            else:
                await github.trigger_workflow_dispatch(owner, repo, DEPLOY_WORKFLOW)

            try:
                run_info = await github.wait_for_workflow_completion(
                    owner=owner,
                    repo=repo,
                    workflow_file=DEPLOY_WORKFLOW,
                    branch=ref,
                    timeout_seconds=DEPLOY_TIMEOUT_SECONDS,
                    created_after=dispatch_time,
                    head_sha=head_sha or None,
                    cancel_check=lambda: self._run_cancelled(run_id),
                )
            except (RuntimeError, TimeoutError) as e:
                if type(e).__name__ in _CANCELLATION_ERRORS:
                    raise
                logger.warning(
                    "deploy_workflow_failed",
                    error=redact_diagnostic(e, secrets=diagnostic_secrets),
                )

                # Attempt to rerun failed jobs (gets a new GH Actions runner)
                rerun_info = await self._try_deploy_rerun(
                    github,
                    owner,
                    repo,
                    dispatch_time,
                    ref,
                    head_sha or None,
                    run_id,
                    diagnostic_secrets,
                )
                if rerun_info is None:
                    raise
                run_info, rerun = rerun_info, True
        finally:
            if pin_tag:
                cleanup_error = await self._remove_pin_tag(
                    github, owner, repo, pin_tag, diagnostic_secrets
                )

        if cleanup_error is not None:
            raise DeployPinTagLeakedError(
                f"deploy pin tag {pin_tag} survived in {owner}/{repo}: {cleanup_error}"
            )
        self._verify_deployed_sha(run_info, head_sha)
        return run_info, rerun

    @staticmethod
    def _verify_deployed_sha(run_info: dict, head_sha: str) -> None:
        """Refuse the deploy unless the finished run is the commit that was asked for."""
        if not head_sha:
            return
        deployed = (run_info.get("head_sha") or "").lower()
        if deployed != head_sha.lower():
            raise DeployedShaMismatchError(
                f"deploy run {run_info['id']} built commit {deployed or 'unknown'}, "
                f"requested {head_sha}"
            )

    def _extract_deploy_params(self, state: DevOpsState) -> dict | None:
        """Extract and validate deployment parameters from state. Returns None on error."""
        project_spec = state.get("project_spec") or {}
        allocated_resources = state.get("allocated_resources", {})

        repo_info = state.get("repo_info") or {}
        repo_url = repo_info.get("html_url", "")
        if not repo_url:
            return None

        parts = repo_url.rstrip("/").split("/")
        deploy_resource = next(
            (
                resource
                for resource in allocated_resources.values()
                if is_http_health_port_service(resource.get("service_name"))
            ),
            {},
        )

        return {
            "owner": parts[-2],
            "repo": parts[-1],
            "project_name": project_spec_runtime_slug(project_spec),
            "server_ip": deploy_resource.get("server_ip"),
            "port": deploy_resource.get("port"),
            "server_handle": deploy_resource.get("server_handle"),
        }

    async def _run_cancelled(self, run_id: str | None) -> bool:
        if not run_id:
            return False
        run = await api_client.get(f"runs/{run_id}")
        return run.get("status") == "cancelled"

    async def _claim_dispatch(self, run_id: str | None) -> DeployDispatchClaim | None:
        """Take the dispatch boundary, or refuse to cross it.

        Called immediately before every call that starts work on GitHub Actions.
        A plain read of the run status cannot do this job: between reading it and
        dispatching, a revoke can cancel the run, see no Actions run to fence,
        clear the value and finish, and only then does this deploy write the
        value back. The claim and that cancellation are decided against the same
        locked row, so one of them loses and knows it.

        Raises:
            DeployDispatchWithdrawnError: the run was stopped first. Nothing was
                dispatched and nothing needs stopping outside.
        """
        if not run_id:
            return None
        claim = await api_client.claim_deploy_dispatch(run_id)
        if not claim.granted:
            logger.info(
                "deploy_dispatch_withdrawn",
                run_id=run_id,
                run_status=claim.run_status.value,
            )
            raise DeployDispatchWithdrawnError(
                f"deploy run {run_id} was {claim.run_status.value} before it was dispatched"
            )
        return claim

    async def run(self, state: DevOpsState) -> dict:  # noqa: PLR0911
        """Build DOTENV, write GitHub secrets, trigger deploy.yml, wait for result."""
        project_id = state.get("project_id")
        run_id = state.get("run_id")
        project_spec = state.get("project_spec") or {}
        secret_values = state.get("secret_values", {})
        diagnostic_secrets = list(_resolved_secret_values(secret_values))
        non_secret_values = state.get("non_secret_values", {})
        # Empty means "deploy whatever main holds now"; a SHA means that exact commit.
        head_sha = state.get("head_sha") or ""
        logger.info("deployer_start", project_id=project_id, head_sha=head_sha)

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

            if await self._run_cancelled(run_id):
                return {"deployment_result": {"status": "cancelled"}}

            # 0. Fetch connection credentials for the same target server.
            server = await api_client.get_server(server_handle) if server_handle else None
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
            all_env = {
                **non_secret_values,
                **secret_values,
                "CODEGEN_PROJECT_ID": project_id,
            }
            dotenv_content = build_dotenv(all_env)
            dotenv_b64 = encode_dotenv(dotenv_content)
            diagnostic_secrets.append(dotenv_b64)

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
                ssh_user=server.ssh_user,
                diagnostic_secrets=diagnostic_secrets,
            )

            if not secrets_ok:
                logger.error(
                    "deploy_secrets_write_failed",
                    server_ip=server_ip,
                    owner=owner,
                    repo=repo,
                )

            # 2.5 Fence older runs when this deploy must be the last writer, and
            # only once the payload above is already the repository's. What the
            # workflow deploys is read from the repository secrets when it runs,
            # not from whoever asked for it, so ordering it this way leaves the
            # smallest window: a run that could read the old payload is on
            # Actions by now and is stopped here, and one created afterwards —
            # a worker resuming past its dispatch lease, above all — reads what
            # this just wrote. Smallest, not none; the caller confirms the
            # removal by reading the deployed service.
            if state.get("fence_active_deploys"):
                try:
                    await self._fence_active_deploys(github, owner, repo, project_id)
                except DeployRefusedError as e:
                    logger.error(
                        "deploy_fence_unproven",
                        project_id=project_id,
                        reason=type(e).__name__,
                        error=redact_diagnostic(e, secrets=diagnostic_secrets),
                    )
                    return {
                        "deployment_result": {
                            "status": "failed",
                            "error": redact_diagnostic(e, secrets=diagnostic_secrets),
                        },
                        "errors": [
                            f"Deploy refused: {redact_diagnostic(e, secrets=diagnostic_secrets)}"
                        ],
                    }

            if await self._run_cancelled(run_id):
                return {"deployment_result": {"status": "cancelled"}}

            # 3. Dispatch deploy.yml and wait for it, pinned to head_sha when one is given
            try:
                run_info, rerun = await self._dispatch_and_wait(
                    github, owner, repo, head_sha, run_id, diagnostic_secrets
                )
            except DeployDispatchWithdrawnError as e:
                # Stopped before anything left the system. Reported as cancelled,
                # like a run stopped on Actions, so the consumer records the same
                # terminal outcome either way.
                logger.info(
                    "deploy_withdrawn_before_dispatch",
                    project_id=project_id,
                    error=redact_diagnostic(e, secrets=diagnostic_secrets),
                )
                return {"deployment_result": {"status": "cancelled"}}
            except DeployRefusedError as e:
                logger.error(
                    "deploy_refused",
                    project_id=project_id,
                    reason=type(e).__name__,
                    error=redact_diagnostic(e, secrets=diagnostic_secrets),
                )
                return {
                    "deployment_result": {
                        "status": "failed",
                        "error": redact_diagnostic(e, secrets=diagnostic_secrets),
                    },
                    "errors": [
                        f"Deploy refused: {redact_diagnostic(e, secrets=diagnostic_secrets)}"
                    ],
                }

            logger.info(
                "deploy_completed",
                owner=owner,
                repo=repo,
                run_id=run_info["id"],
                head_sha=run_info.get("head_sha"),
                rerun=rerun,
            )

            # 4. Create service deployment record
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
                    "env_overrides_digest": env_overrides_digest(state.get("env_overrides")),
                },
                deployed_sha=run_info.get("head_sha"),
                diagnostic_secrets=diagnostic_secrets,
            )

            deployed_url = f"http://{server_ip}:{port}"
            suffix = " (after rerun)" if rerun else ""
            return {
                "deployment_result": {"status": "success", "run_id": run_info["id"]},
                "deployed_url": deployed_url,
                "application_id": application_id,
                "messages": [
                    AIMessage(content=f"Deployment successful{suffix}! URL: {deployed_url}")
                ],
            }

        except (RuntimeError, TimeoutError) as e:
            if type(e).__name__ in _CANCELLATION_ERRORS:
                if type(e).__name__ == "WorkflowCancellationUnprovenError":
                    raise
                logger.info("deploy_workflow_cancelled", project_id=project_id, run_id=run_id)
                return {"deployment_result": {"status": "cancelled"}}

            error_prefix = (
                "Deploy timeout" if isinstance(e, TimeoutError) else "Deploy workflow failed"
            )
            return {
                "deployment_result": {
                    "status": "failed",
                    "error": redact_diagnostic(e, secrets=diagnostic_secrets),
                },
                "errors": [f"{error_prefix}: {redact_diagnostic(e, secrets=diagnostic_secrets)}"],
            }

        except Exception as e:
            logger.error(
                "deployer_failed",
                error=redact_diagnostic(e, secrets=diagnostic_secrets),
            )
            return {
                "deployment_result": {
                    "status": "error",
                    "error": redact_diagnostic(e, secrets=diagnostic_secrets),
                },
                "errors": [f"Deployment error: {redact_diagnostic(e, secrets=diagnostic_secrets)}"],
            }
