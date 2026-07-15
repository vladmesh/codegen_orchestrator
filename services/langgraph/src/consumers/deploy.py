"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Pure technical worker: only updates run.status and run.result.
Story lifecycle transitions are handled by the dispatcher.

Run standalone: python -m src.consumers.deploy
"""

from __future__ import annotations

import structlog

from shared.config_store import ConfigStore
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployOutcome
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..subgraphs.devops import create_devops_subgraph
from ..tracing import build_langfuse_metadata, get_langfuse_callbacks
from ._base import start_worker, validate_queued_message
from ._events import publish_callback_event
from .deploy_failure_handler import (
    CLASSIFY_PROMPT,
    _classification_to_outcome,
    _classify_deploy_failure,
    _handle_deploy_failure,
)
from .deploy_lifecycle import process_lifecycle_action
from .deploy_precheck import (
    SERVICE_BASE_DIR,
    _pre_check_server,
    _run_deploy_precheck,
)
from .deploy_result_handler import (
    _handle_deploy_success,
    _handle_smoke_failure,
)

# Re-export for backward compatibility with tests
__all__ = [
    "CLASSIFY_PROMPT",
    "SERVICE_BASE_DIR",
    "_build_subgraph_input",
    "_classify_deploy_failure",
    "_handle_deploy_failure",
    "_handle_deploy_success",
    "_handle_smoke_failure",
    "_pre_check_server",
    "_run_deploy_precheck",
    "process_deploy_job",
]

logger = structlog.get_logger(__name__)

_config: ConfigStore | None = None

_DEPLOY_INFRA_PORT_SERVICES = ("postgres", "redis")


def _deploy_lock_ttl() -> int:
    global _config  # noqa: PLW0603
    if _config is None:
        import os

        api_base_url = os.getenv("API_BASE_URL")
        if not api_base_url:
            raise RuntimeError("API_BASE_URL is not set")
        _config = ConfigStore(api_base_url)
    return _config.get_int("deploy.deploy_lock_ttl", default=3600)


async def _allocate_resources(project_id: str, project: ProjectDTO) -> dict | str:
    """Get or create allocations. Returns dict of resources or error string."""
    from ..allocations import AllocationError, ensure_project_allocations

    try:
        config = project.config or {}
        modules = list(
            dict.fromkeys([*config.get("modules", ["backend"]), *_DEPLOY_INFRA_PORT_SERVICES])
        )
        min_ram_mb = config.get("estimated_ram_mb", 512)

        # Get repo_id from primary repository
        primary_repo = await api_client.get_primary_repository(project_id)
        if not primary_repo:
            return f"No repository found for project {project_id}"
        repo_id = primary_repo.id
        service_name = project.name

        return await ensure_project_allocations(
            project_id=project_id,
            repo_id=repo_id,
            service_name=service_name,
            modules=modules,
            min_ram_mb=min_ram_mb,
        )
    except AllocationError as e:
        return str(e)


def _build_subgraph_input(
    project_id: str,
    project: ProjectDTO,
    git_url: str,
    allocated_resources: dict,
    job_data: dict,
    head_sha: str = "",
) -> dict:
    """Build DevOps subgraph input from deploy job data."""
    return {
        "project_id": project_id,
        "run_id": job_data.get("task_id"),
        "project_spec": project.model_dump(),
        "repo_info": {
            "full_name": git_url.replace("https://github.com/", "")
            .rstrip("/")
            .removesuffix(".git"),
            "html_url": git_url,
        },
        "allocated_resources": allocated_resources,
        "provided_secrets": job_data.get("provided_secrets", {}),
        "head_sha": head_sha or None,
        "messages": [],
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": {},
        "missing_user_secrets": [],
        "deployment_result": None,
        "deployed_url": None,
        "smoke_result": None,
        "errors": [],
    }


async def _handle_lifecycle_action(
    msg: DeployMessage,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
    allocated_resources: dict,
) -> dict:
    """Handle stop/undeploy lifecycle actions — SSH only, no DevOps subgraph."""
    project_name = (project.name or project_id).replace(" ", "_").lower()
    lifecycle_result = await process_lifecycle_action(
        action=msg.action,
        task_id=task_id,
        project_id=project_id,
        project_name=project_name,
        allocated_resources=allocated_resources,
    )
    run_status = (
        RunStatus.COMPLETED if lifecycle_result["status"] == "success" else RunStatus.FAILED
    )
    run_result = DeployRunResult(
        deploy_outcome=lifecycle_result["deploy_outcome"],
        action=msg.action,
    )
    run_patch: dict = {
        "status": run_status.value,
        "result": run_result.model_dump(mode="json"),
    }
    if lifecycle_result.get("error"):
        run_patch["error_message"] = lifecycle_result["error"]
    await api_client.patch(f"runs/{task_id}", json=run_patch)

    # Update application status on success
    if lifecycle_result["status"] == "success":
        app_id = next(iter(allocated_resources.values()))["application_id"]
        target_status = (
            ApplicationStatus.NOT_DEPLOYED
            if msg.action == DeployAction.UNDEPLOY
            else ApplicationStatus.STOPPED
        )
        await api_client.patch(
            f"applications/{app_id}",
            json={"status": target_status.value},
        )

    return lifecycle_result


async def process_deploy_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single deploy job by running DevOps Subgraph."""
    msg = validate_queued_message(DeployMessage, job_data)
    task_id = msg.task_id
    project_id = msg.project_id
    story_id = msg.story_id
    callback_stream = msg.callback_stream
    user_id = msg.user_id

    logger.info(
        "deploy_job_started",
        task_id=task_id,
        project_id=project_id,
        triggered_by=msg.triggered_by.value,
    )

    lock_key = f"deploy:{project_id}:lock"

    try:
        # Atomic Redis lock: only one consumer can process a deploy per project
        acquired = await redis.redis.set(lock_key, task_id, nx=True, ex=_deploy_lock_ttl())
        if not acquired:
            logger.info(
                "deploy_lock_not_acquired",
                task_id=task_id,
                project_id=project_id,
                lock_key=lock_key,
            )
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.CANCELLED.value,
                    "error_message": (
                        f"Skipped: another deploy is already in progress for project {project_id}"
                    ),
                },
            )
            return {"status": "cancelled", "reason": "deploy_lock_held"}

        # Update task status to running
        await api_client.patch(f"runs/{task_id}", json={"status": RunStatus.RUNNING.value})

        # Publish progress event
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Deploy task started",
            user_id=user_id,
            project_id=project_id or "",
        )

        # Fetch project details (with user isolation)
        tg_kwargs = {"telegram_id": int(user_id)} if user_id and user_id.isdigit() else {}
        project: ProjectDTO | None = await api_client.get_project(project_id, **tg_kwargs)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.FAILED.value,
                    "error_message": error_msg,
                    "result": DeployRunResult(deploy_outcome=DeployOutcome.GIVE_UP).model_dump(
                        mode="json"
                    ),
                },
            )
            return {"status": "failed", "error": error_msg}

        # Get or create allocations for the project
        alloc_result = await _allocate_resources(project_id, project)
        if isinstance(alloc_result, str):
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.FAILED.value,
                    "error_message": alloc_result,
                    "result": DeployRunResult(deploy_outcome=DeployOutcome.GIVE_UP).model_dump(
                        mode="json"
                    ),
                },
            )
            return {"status": "failed", "error": alloc_result}
        allocated_resources = alloc_result

        # Lifecycle actions (stop/undeploy) — skip DevOps subgraph entirely
        if msg.action in (DeployAction.STOP, DeployAction.UNDEPLOY):
            return await _handle_lifecycle_action(
                msg, task_id, project_id, project, allocated_resources
            )

        # Pre-check: validate server state via SSH before deploying
        action = msg.action
        precheck_error = await _run_deploy_precheck(
            allocated_resources, project, project_id, action
        )

        # Auto-fallback: create ↔ feature based on actual server state
        if precheck_error and action == "create" and "already exists" in precheck_error:
            logger.warning(
                "deploy_action_auto_fallback",
                task_id=task_id,
                from_action="create",
                to_action="feature",
                reason=precheck_error,
            )
            action = "feature"
            precheck_error = await _run_deploy_precheck(
                allocated_resources, project, project_id, action
            )
        elif precheck_error and action == "feature" and "never deployed" in precheck_error:
            logger.warning(
                "deploy_action_auto_fallback",
                task_id=task_id,
                from_action="feature",
                to_action="create",
                reason=precheck_error,
            )
            action = "create"
            precheck_error = await _run_deploy_precheck(
                allocated_resources, project, project_id, action
            )

        if precheck_error:
            logger.warning("deploy_precheck_failed", task_id=task_id, error=precheck_error)
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=precheck_error,
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

        # Resolve git_url from primary Repository entity
        primary_repo = await api_client.get_primary_repository(project_id)
        _git_url = primary_repo.git_url if primary_repo else ""

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()
        subgraph_input = _build_subgraph_input(
            project_id,
            project,
            _git_url,
            allocated_resources,
            job_data,
            head_sha=msg.head_sha,
        )
        result = await devops_subgraph.ainvoke(
            subgraph_input,
            config={
                "callbacks": get_langfuse_callbacks(),
                "metadata": build_langfuse_metadata(
                    agent_type="deploy",
                    user_id=user_id,
                    project_id=project_id,
                    task_id=task_id,
                    story_id=story_id,
                ),
            },
        )

        logger.info(
            "devops_subgraph_result",
            task_id=task_id,
            result_keys=sorted(result.keys()),
            has_smoke_result="smoke_result" in result,
            smoke_result=result.get("smoke_result"),
            deployed_url=result.get("deployed_url"),
            errors=result.get("errors"),
        )

        if result.get("deployment_result", {}).get("status") == "cancelled":
            logger.info("deploy_job_cancelled_during_actions", task_id=task_id)
            return {"status": "cancelled"}

        if result.get("deployed_url"):
            smoke_result = result.get("smoke_result")
            smoke_failed = smoke_result and smoke_result.get("status") == "fail"

            if smoke_failed:
                project_name = project.name if project else project_id
                return await _handle_smoke_failure(
                    result=result,
                    smoke_result=smoke_result,
                    task_id=task_id,
                    project_id=project_id,
                    project_name=project_name,
                    callback_stream=callback_stream,
                    user_id=user_id,
                    story_id=story_id,
                    redis=redis,
                    msg=msg,
                )

            return await _handle_deploy_success(
                result=result,
                smoke_result=smoke_result,
                task_id=task_id,
                project_id=project_id,
                project=project,
                callback_stream=callback_stream,
                user_id=user_id,
                story_id=story_id,
                redis=redis,
                application_id=result.get("application_id"),
            )
        elif result.get("missing_user_secrets"):
            missing = result.get("missing_user_secrets")
            logger.info("deploy_job_missing_secrets", task_id=task_id, missing=missing)
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=f"Missing secrets: {', '.join(missing)}",
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
                deploy_outcome=DeployOutcome.GIVE_UP,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )
        else:
            errors = result.get("errors", ["Unknown deployment error"])
            logger.error("deploy_job_failed", task_id=task_id, errors=errors)
            error_msg = "; ".join(errors)

            # Classify failure and store outcome for dispatcher
            classification = await _classify_deploy_failure(error_msg)
            deploy_outcome = _classification_to_outcome(classification)

            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=error_msg,
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
                deploy_outcome=deploy_outcome,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

    except Exception as e:
        logger.error(
            "deploy_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return await _handle_deploy_failure(
            task_id=task_id,
            project_id=project_id,
            story_id=story_id,
            error_msg=str(e),
            callback_stream=callback_stream,
            user_id=user_id,
            redis=redis,
            deploy_fix_attempt=msg.deploy_fix_attempt,
        )
    finally:
        # Always release the deploy lock so the next deploy can proceed
        await redis.redis.delete(lock_key)


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="deploy-worker",
        queue=DEPLOY_QUEUE,
        process_fn=process_deploy_job,
    )


if __name__ == "__main__":
    main()
