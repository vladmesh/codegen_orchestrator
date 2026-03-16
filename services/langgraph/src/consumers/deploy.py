"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Run standalone: python -m src.consumers.deploy
"""

from __future__ import annotations

from datetime import UTC, datetime
import os

import asyncssh
from langchain_openai import ChatOpenAI
import structlog

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.qa import QAMessage
from shared.notifications import notify_admins
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, QA_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import clear_story_worker, get_story_worker
from ..clients.worker_spawner import delete_worker
from ..schemas.api_types import ProjectInfo
from ..subgraphs.devops import create_devops_subgraph
from ..tracing import build_langfuse_metadata, get_langfuse_callbacks
from ._base import start_worker
from ._events import publish_callback_event

logger = structlog.get_logger(__name__)

SERVICE_BASE_DIR = "/opt/services"
MAX_DEPLOY_FIX_ATTEMPTS = 2
MAX_DEPLOY_RETRIES = 3  # max deploy failures before story is marked failed
DEPLOY_LOCK_TTL = 3600  # 1 hour — generous TTL for long deploys
DEPLOY_RETRY_TTL = 86400  # 24h — counter expires after a day

CLASSIFY_PROMPT = """\
Classify this deployment failure into one of three categories.

CODE_FIX = application bug that a developer can fix by changing code \
(import error, crash, missing dependency, wrong config value, syntax error, \
broken migration SQL, unhandled exception at startup, test failure)
RETRY = transient infrastructure issue that may self-resolve on retry \
(SSH timeout, healthcheck slow start, network unreachable temporarily, \
Docker pull timeout, DNS resolution timeout, brief resource contention)
GIVE_UP = persistent infrastructure or configuration issue that will NOT self-heal and \
cannot be fixed by changing code (port already in use/allocated, disk full, \
server out of memory, misconfigured secrets, SSL certificate error, \
permanent DNS failure, firewall blocking, container runtime broken)

Error details:
{error_details}

Reply with exactly one word: CODE_FIX, RETRY, or GIVE_UP"""


async def _classify_deploy_failure(error_details: str) -> str:
    """Use LLM to classify a deploy failure.

    Returns "CODE_FIX", "RETRY", or "GIVE_UP". Defaults to "RETRY" on any error
    (safer than CODE_FIX — retrying wastes less time than dispatching a useless worker).
    """
    try:
        api_key = os.environ.get("OPEN_ROUTER_KEY")
        if not api_key:
            logger.warning("deploy_classify_no_api_key")
            return "RETRY"

        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="anthropic/claude-haiku-4-5",
            temperature=0.0,
            max_tokens=10,
        )
        response = await llm.ainvoke(CLASSIFY_PROMPT.format(error_details=error_details[:2000]))
        classification = response.content.strip().upper()

        valid_classifications = ("CODE_FIX", "RETRY", "GIVE_UP")
        if classification not in valid_classifications:
            logger.warning("deploy_classify_unexpected", raw=classification)
            return "RETRY"

        logger.info(
            "deploy_failure_classified",
            classification=classification,
            error_preview=error_details[:200],
        )
        return classification
    except Exception:
        logger.warning("deploy_classify_error", exc_info=True)
        return "RETRY"


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


async def _handle_give_up(
    *,
    story_id: str,
    task_id: str,
    project_id: str,
    error_details: str,
    redis: RedisStreamClient,
) -> None:
    """Handle GIVE_UP classification — terminal failure, admin notified.

    The deploy failure is a persistent config/infra issue that won't self-heal
    and can't be fixed by changing code. Stop the pipeline and escalate.
    """
    logger.warning(
        "deploy_give_up",
        task_id=task_id,
        project_id=project_id,
        story_id=story_id,
        error_preview=error_details[:200],
    )

    # Story → failed (terminal)
    await _transition_story_safe(story_id, "fail")

    # Clean up worker if one exists
    if story_id:
        try:
            worker_id = await get_story_worker(redis.redis, story_id)
            if worker_id:
                await delete_worker(worker_id, reason="failed")
                await clear_story_worker(redis.redis, story_id)
        except Exception:
            logger.warning("give_up_worker_cleanup_failed", story_id=story_id, exc_info=True)

    # Notify admin (HITL required)
    try:
        await notify_admins(
            f"Deploy GIVE_UP for task {task_id} (project {project_id}):\n{error_details[:500]}",
            level="error",
        )
    except Exception:
        logger.warning("give_up_admin_notify_failed", task_id=task_id, exc_info=True)


async def _route_deploy_failure(
    *,
    classification: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
    error_details: str,
    story_id: str,
) -> None:
    """Route a deploy failure based on three-way classification.

    CODE_FIX → redispatch to engineering worker
    RETRY → do nothing (caller handles retry counter via _handle_deploy_failure)
    GIVE_UP → terminal failure, escalate to admin
    """
    if classification == "CODE_FIX":
        await _transition_story_safe(story_id, "start")
        await _redispatch_to_engineering(
            redis=redis,
            msg=msg,
            error_details=error_details,
        )
    elif classification == "GIVE_UP":
        await _handle_give_up(
            story_id=story_id,
            task_id=msg.task_id,
            project_id=msg.project_id,
            error_details=error_details,
            redis=redis,
        )
    # RETRY: caller handles via _handle_deploy_failure / _track_deploy_retry


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
    # Classify and route failure
    classification = await _classify_deploy_failure(smoke_details)
    await _route_deploy_failure(
        classification=classification,
        redis=redis,
        msg=msg,
        error_details=smoke_details,
        story_id=story_id,
    )
    # For RETRY, also track via retry counter
    if classification == "RETRY":
        await _track_deploy_retry(redis=redis, story_id=story_id)

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id,
    )
    # No proactive message — smoke failure is internal (retried or redispatched)

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
    # Hand off to QA if story exists, otherwise complete directly
    if story_id:
        await _transition_story_safe(story_id, "test")
        await redis.publish_message(
            QA_QUEUE,
            QAMessage(
                story_id=story_id,
                project_id=project_id,
                user_id=user_id,
                deployed_url=result["deployed_url"],
            ),
        )
        logger.info("qa_handoff", story_id=story_id, deployed_url=result["deployed_url"])
        # Worker container NOT deleted — QA may need it for fix tasks
    else:
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            f"Deploy completed: {result['deployed_url']}",
            user_id=user_id,
            project_id=project_id,
        )

    return {
        "status": "success",
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _track_deploy_retry(*, redis: RedisStreamClient, story_id: str) -> None:
    """Increment deploy retry counter and transition story.

    After MAX_DEPLOY_RETRIES failures, marks story as failed (HITL).
    Otherwise rolls story back to "start" for another deploy attempt.
    """
    if not story_id:
        await _transition_story_safe(story_id, "start")
        return

    attempt_key = f"deploy:{story_id}:attempts"
    attempts = await redis.redis.incr(attempt_key)
    await redis.redis.expire(attempt_key, DEPLOY_RETRY_TTL)

    if attempts >= MAX_DEPLOY_RETRIES:
        logger.warning(
            "deploy_max_retries_exceeded",
            story_id=story_id,
            attempts=attempts,
            max_retries=MAX_DEPLOY_RETRIES,
        )
        await _transition_story_safe(story_id, "fail")
        try:
            worker_id = await get_story_worker(redis.redis, story_id)
            if worker_id:
                await delete_worker(worker_id, reason="failed")
                logger.info(
                    "story_worker_deleted_on_fail",
                    story_id=story_id,
                    worker_id=worker_id,
                )
            await clear_story_worker(redis.redis, story_id)
        except Exception as e:
            logger.warning("story_worker_cleanup_failed", story_id=story_id, error=str(e))
    else:
        logger.info(
            "deploy_failure_rollback",
            story_id=story_id,
            attempt=attempts,
            max_retries=MAX_DEPLOY_RETRIES,
        )
        await _transition_story_safe(story_id, "start")


async def _handle_deploy_failure(
    *,
    task_id: str,
    project_id: str,
    error_msg: str,
    story_id: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
) -> dict:
    """Common handler for deploy failures — update run, rollback story, notify.

    Tracks consecutive deploy failures per story in Redis. After MAX_DEPLOY_RETRIES
    failures, transitions story to failed instead of back to in_progress (prevents
    infinite deploy-fail-retry loops).
    """
    await api_client.patch(
        f"runs/{task_id}",
        json={"status": "failed", "error_message": error_msg},
    )
    await _track_deploy_retry(redis=redis, story_id=story_id)

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

        # Get repo_id from primary repository
        primary_repo = await api_client.get_primary_repository(project_id)
        if not primary_repo:
            return f"No repository found for project {project_id}"
        repo_id = primary_repo["id"]
        service_name = project.get("name", project_id)

        return await ensure_project_allocations(
            project_id=project_id,
            repo_id=repo_id,
            service_name=service_name,
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
            )

        # Resolve git_url from primary Repository entity
        primary_repo = await api_client.get_primary_repository(project_id)
        _git_url = primary_repo.get("git_url", "") if primary_repo else ""

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()
        subgraph_input = _build_subgraph_input(
            project_id, project, _git_url, allocated_resources, job_data
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

            # Classify and route failure
            classification = await _classify_deploy_failure(error_msg)
            await _route_deploy_failure(
                classification=classification,
                redis=redis,
                msg=msg,
                error_details=error_msg,
                story_id=story_id,
            )
            # GIVE_UP already handled (story failed, admin notified) — skip retry
            if classification == "GIVE_UP":
                return {
                    "status": "failed",
                    "error": error_msg,
                    "classification": "GIVE_UP",
                    "finished_at": datetime.now(UTC).isoformat(),
                }

            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=error_msg,
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
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
