"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Run standalone: python -m src.consumers.deploy
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncssh
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.engineering import EngineeringMessage
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..schemas.api_types import ProjectInfo
from ..subgraphs.devops import create_devops_subgraph
from ._base import start_worker
from ._events import publish_callback_event, publish_proactive_message

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"
MAX_DEPLOY_FIX_ATTEMPTS = 2
DEPLOY_LOCK_TTL = 3600  # 1 hour — generous TTL for long deploys


async def _transition_story_safe(story_id: str, action: str) -> None:
    """Transition story status, logging errors without raising."""
    if not story_id:
        return
    try:
        await api_client.transition_story(story_id, action)
        logger.info("story_transitioned", story_id=story_id, action=action)
    except Exception:
        logger.warning("story_transition_failed", story_id=story_id, action=action, exc_info=True)


async def _pre_check_server(
    server_ip: str,
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
    service_dir = f"{SERVICE_BASE_DIR}/{project_name}/"

    try:
        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username="root",
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            result = await conn.run(f"test -d {service_dir}", check=False)
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
        project_name=project_name,
        action=action,
        dir_exists=dir_exists,
    )
    return None


async def _redispatch_to_engineering(
    *,
    redis: RedisStreamClient,
    msg: DeployMessage,
    error_details: str,
) -> bool:
    """Re-dispatch a fix task to engineering when deploy fails due to a code bug.

    Returns True if re-dispatched, False if retry limit reached.
    """
    attempt = msg.deploy_fix_attempt
    if attempt >= MAX_DEPLOY_FIX_ATTEMPTS:
        logger.warning(
            "deploy_fix_retries_exhausted",
            task_id=msg.task_id,
            project_id=msg.project_id,
            attempt=attempt,
        )
        return False

    fix_task_id = f"eng-deploy-fix-{msg.task_id}-{attempt + 1}"

    # Create a run record for the fix task
    try:
        await api_client.post(
            "runs/",
            json={
                "id": fix_task_id,
                "type": RunType.ENGINEERING.value,
                "project_id": msg.project_id,
                "status": RunStatus.QUEUED.value,
            },
        )
    except Exception:
        logger.warning("deploy_fix_run_create_failed", fix_task_id=fix_task_id, exc_info=True)

    fix_msg = EngineeringMessage(
        task_id=fix_task_id,
        project_id=msg.project_id,
        user_id=msg.user_id,
        action="fix",
        description=(
            f"Deploy failed — fix the code so containers start cleanly.\n\n"
            f"Error: {error_details}\n\n"
            f"Run the service locally or check imports/dependencies before pushing."
        ),
        skip_deploy=False,
        story_id=msg.story_id or None,
        deploy_fix_attempt=attempt + 1,
    )

    await redis.publish_message(ENGINEERING_QUEUE, fix_msg)
    logger.info(
        "deploy_fix_redispatched",
        fix_task_id=fix_task_id,
        project_id=msg.project_id,
        attempt=attempt + 1,
    )
    return True


async def _handle_smoke_failure(
    *,
    result: dict,
    smoke_result: dict,
    task_id: str,
    project_id: str,
    project_name: str,
    callback_stream: str,
    user_id: str,
    story_id: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
) -> dict:
    """Handle deploy success with smoke test failure."""
    smoke_details = "; ".join(
        f"{c['module']}: {c['detail']}"
        for c in smoke_result.get("checks", [])
        if c.get("result") == "fail"
    )
    error_msg = f"Deployed but smoke test failed: {smoke_details}"
    logger.warning(
        "deploy_job_smoke_failed",
        task_id=task_id,
        deployed_url=result["deployed_url"],
        smoke_details=smoke_details,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "failed",
            "error_message": error_msg,
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    # Project stays active — deploy succeeded, service just unhealthy
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": ProjectStatus.ACTIVE.value},
    )

    # Roll story back — smoke failed, story not truly complete
    await _transition_story_safe(story_id, "start")

    # Re-dispatch to engineering for a code fix
    await _redispatch_to_engineering(
        redis=redis,
        msg=msg,
        error_details=smoke_details,
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id,
    )
    # No proactive message — smoke failure is internal (redispatched to engineering)

    return {
        "status": "failed",
        "error": error_msg,
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _handle_deploy_success(
    *,
    result: dict,
    smoke_result: dict | None,
    task_id: str,
    project_id: str,
    project: dict,
    callback_stream: str,
    user_id: str,
    story_id: str,
    redis: RedisStreamClient,
) -> dict:
    """Handle successful deploy (with or without smoke)."""
    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "completed",
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": ProjectStatus.ACTIVE.value},
    )

    # Complete the story now that deploy succeeded
    await _transition_story_safe(story_id, "complete")

    await publish_callback_event(
        redis,
        callback_stream,
        "completed",
        task_id,
        f"Deploy completed: {result['deployed_url']}",
        user_id=user_id,
        project_id=project_id,
    )
    if not callback_stream:
        project_name = project.get("name", project_id) if project else project_id
        await publish_proactive_message(
            redis, user_id, f"Deployed {project_name}: {result['deployed_url']}"
        )

    return {
        "status": "success",
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _handle_deploy_failure(
    *,
    task_id: str,
    project_id: str,
    error_msg: str,
    story_id: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
    rollback_project: bool = True,
) -> dict:
    """Common handler for deploy failures — update run, rollback story, notify."""
    await api_client.patch(
        f"runs/{task_id}",
        json={"status": "failed", "error_message": error_msg},
    )
    if rollback_project:
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.FAILED.value},
        )
    await _transition_story_safe(story_id, "start")

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id or "",
    )
    # No proactive message — deploy failures are internal (retried automatically)

    return {
        "status": "failed",
        "error": error_msg,
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _allocate_resources(project_id: str, project: dict) -> dict | str:
    """Get or create allocations. Returns dict of resources or error string."""
    from ..tools.allocator import AllocationError, ensure_project_allocations

    try:
        config = project.get("config", {})
        modules = config.get("modules", ["backend"])
        min_ram_mb = config.get("estimated_ram_mb", 512)
        return await ensure_project_allocations(
            project_id=project_id,
            modules=modules,
            min_ram_mb=min_ram_mb,
        )
    except AllocationError as e:
        return str(e)


async def _run_deploy_precheck(
    allocated_resources: dict, project: dict, project_id: str, action: str
) -> str | None:
    """Run SSH pre-check against the target server. Returns error or None."""
    first_resource = next(iter(allocated_resources.values()), {})
    if not isinstance(first_resource, dict):
        return None
    server_ip = first_resource.get("server_ip")
    server_handle = first_resource.get("server_handle")
    if not server_ip or not server_handle:
        return None

    project_name = (project.get("name") or project_id).replace(" ", "_").lower()
    ssh_key = await api_client.get_server_ssh_key(server_handle)
    if not ssh_key:
        return None

    return await _pre_check_server(
        server_ip=server_ip,
        ssh_key=ssh_key,
        project_name=project_name,
        action=action,
    )


def _build_subgraph_input(
    project_id: str, project: dict, git_url: str, allocated_resources: dict, job_data: dict
) -> dict:
    """Build DevOps subgraph input from deploy job data."""
    return {
        "project_id": project_id,
        "project_spec": project,
        "repo_info": {
            "full_name": git_url.replace("https://github.com/", "")
            .rstrip("/")
            .removesuffix(".git"),
            "html_url": git_url,
        },
        "allocated_resources": allocated_resources,
        "provided_secrets": job_data.get("provided_secrets", {}),
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


async def process_deploy_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single deploy job by running DevOps Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    msg = DeployMessage.model_validate(job_data)
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
        acquired = await redis.redis.set(lock_key, task_id, nx=True, ex=DEPLOY_LOCK_TTL)
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
        project: ProjectInfo | None = await api_client.get_project(project_id, **tg_kwargs)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Get or create allocations for the project
        alloc_result = await _allocate_resources(project_id, project)
        if isinstance(alloc_result, str):
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": alloc_result},
            )
            return {"status": "failed", "error": alloc_result}
        allocated_resources = alloc_result

        # Pre-check: validate server state via SSH before deploying
        action = msg.action
        precheck_error = await _run_deploy_precheck(
            allocated_resources, project, project_id, action
        )

        # Auto-fallback: create → feature when dir already exists
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
                rollback_project=False,
            )

        # Update project status to deploying
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEPLOYING.value},
        )

        # Resolve git_url from primary Repository entity
        primary_repo = await api_client.get_primary_repository(project_id)
        _git_url = primary_repo.get("git_url", "") if primary_repo else ""

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()
        subgraph_input = _build_subgraph_input(
            project_id, project, _git_url, allocated_resources, job_data
        )
        result = await devops_subgraph.ainvoke(subgraph_input)

        logger.info(
            "devops_subgraph_result",
            task_id=task_id,
            result_keys=sorted(result.keys()),
            has_smoke_result="smoke_result" in result,
            smoke_result=result.get("smoke_result"),
            deployed_url=result.get("deployed_url"),
            errors=result.get("errors"),
        )

        if result.get("deployed_url"):
            smoke_result = result.get("smoke_result")
            smoke_failed = smoke_result and smoke_result.get("status") == "fail"

            if smoke_failed:
                project_name = project.get("name", project_id) if project else project_id
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
            )
        elif result.get("missing_user_secrets"):
            missing = result.get("missing_user_secrets")
            logger.info("deploy_job_missing_secrets", task_id=task_id, missing=missing)
            project_name = project.get("name", project_id) if project else project_id
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=f"Missing secrets: {', '.join(missing)}",
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
            )
        else:
            errors = result.get("errors", ["Unknown deployment error"])
            logger.error("deploy_job_failed", task_id=task_id, errors=errors)
            error_msg = "; ".join(errors)
            project_name = project.get("name", project_id) if project else project_id

            # Re-dispatch to engineering for a code fix
            await _redispatch_to_engineering(
                redis=redis,
                msg=msg,
                error_details=error_msg,
            )

            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=error_msg,
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
                rollback_project=False,
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
