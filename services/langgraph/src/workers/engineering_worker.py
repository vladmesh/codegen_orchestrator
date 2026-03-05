"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from shared.clients.github import GitHubAppClient

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.worker_spawner import delete_worker, send_task_to_worker
from ..nodes.resource_allocator import resource_allocator_node
from ._base import start_worker
from ._events import publish_callback_event


def _parse_telegram_id(user_id: str) -> dict:
    """Build get_project kwargs with telegram_id if user_id is numeric."""
    if user_id and user_id.isdigit():
        return {"telegram_id": int(user_id)}
    return {}


logger = structlog.get_logger(__name__)


# Markers indicating infrastructure / config CI failures that a developer worker cannot fix.
_INFRA_FAILURE_MARKERS = [
    "Docker Registry",
    "Log in to",
    "docker login",
    "connection refused",
    "registry",
    "TLS handshake",
    "certificate",
    "REGISTRY_",
    "DEPLOY_",
    "SSH",
    "deploy",
    "Could not resolve host",
]


def _is_infra_failure(failure_context: str) -> bool:
    """Return True if the CI failure is an infrastructure/config issue.

    Infrastructure failures (registry auth, TLS, deploy secrets, network)
    cannot be fixed by a developer worker — only by an admin.
    """
    ctx_lower = failure_context.lower()
    return any(marker.lower() in ctx_lower for marker in _INFRA_FAILURE_MARKERS)


def _extract_run_id_from_error(error_msg: str) -> int | None:
    """Extract workflow run ID from URL in RuntimeError message.

    The wait_for_workflow_completion raises RuntimeError with format:
    'Workflow ci.yml failed: failure. See: https://github.com/.../actions/runs/12345'
    """
    match = re.search(r"/actions/runs/(\d+)", error_msg)
    return int(match.group(1)) if match else None


def _build_ci_fix_prompt(failure_context: str, attempt: int) -> str:
    """Build the prompt for CI fix task (used by both reuse and respawn paths)."""
    return f"""# Task: Fix CI Failures (Attempt {attempt})

## CI Failure Details

{failure_context or "CI workflow failed. Check the CI logs for details."}

## Instructions

1. The repository is already cloned to `/workspace`. Pull latest changes with `git pull`.
2. Analyze the CI failure details above to understand the root cause.
   - Use `gh run list --branch main` and `gh run view <run-id> --log` for full CI logs.
3. Fix the root cause of the failure.
4. Run any relevant checks locally (linting, tests) to verify your fix.
5. Commit and push your fixes.

## Important

- Focus ONLY on fixing the CI failures, do not add new features.
- Make a descriptive commit message explaining what you fixed.
"""


async def _respawn_developer_for_ci_fix(
    project: dict,
    owner: str,
    repo_name: str,
    repo_full_name: str,
    github_client: GitHubAppClient,
    failure_context: str,
    attempt: int,
) -> bool:
    """Spawn a new developer worker to fix CI failures.

    Returns:
        True if developer completed successfully, False otherwise
    """
    from ..clients.worker_spawner import request_spawn
    from ..config.constants import Timeouts

    access_token = await github_client.get_token(owner, repo_name)
    project_name = project.get("name", "project")
    task_message = _build_ci_fix_prompt(failure_context, attempt)

    worker_result = await request_spawn(
        repo=repo_full_name,
        github_token=access_token,
        task_content=task_message,
        task_title=f"Fix CI failures for {project_name} (attempt {attempt})",
        timeout_seconds=Timeouts.WORKER_SPAWN,
        project_id=project.get("id"),
    )

    return worker_result.success


async def _try_infra_rerun(
    github_client: GitHubAppClient,
    owner: str,
    repo_name: str,
    run_id: int,
    task_id: str,
    timeout_seconds: int,
    redis: RedisStreamClient,
    callback_stream: str | None,
    user_id: str,
    project_id: str,
) -> bool | None:
    """Attempt to rerun failed CI jobs for an infrastructure failure.

    Returns:
        True if rerun succeeded, False if rerun completed but still failed,
        None if the rerun API call itself errored (caller should fall through).
    """
    try:
        logger.info("ci_infra_rerun_attempting", task_id=task_id, run_id=run_id)
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "CI infra failure detected, rerunning...",
            user_id=user_id,
            project_id=project_id,
        )
        await github_client.rerun_failed_jobs(owner, repo_name, run_id)
        await asyncio.sleep(3)  # let GitHub update run status
        await github_client.wait_for_run_completion(
            owner,
            repo_name,
            run_id,
            timeout_seconds=timeout_seconds,
        )
        logger.info("ci_infra_rerun_passed", task_id=task_id, run_id=run_id)
        return True
    except (RuntimeError, TimeoutError) as e:
        logger.error(
            "ci_infra_rerun_failed",
            task_id=task_id,
            run_id=run_id,
            error=str(e),
        )
        return False
    except Exception as e:
        logger.error(
            "ci_infra_rerun_api_error",
            task_id=task_id,
            run_id=run_id,
            error=str(e),
        )
        return None


async def _attempt_developer_fix(
    worker_id: str | None,
    project: dict,
    owner: str,
    repo_name: str,
    repo_full_name: str,
    github_client: GitHubAppClient,
    failure_context: str,
    attempt: int,
    task_id: str,
) -> tuple[bool, str | None]:
    """Try to fix a CI code failure via an existing worker or by respawning.

    Returns:
        Tuple of (success, updated_worker_id). worker_id may become None
        if the existing worker timed out and a fresh respawn was used.
    """
    from ..config.constants import Timeouts

    task_message = _build_ci_fix_prompt(failure_context, attempt)

    if worker_id:
        logger.info(
            "ci_fix_reuse_worker",
            task_id=task_id,
            worker_id=worker_id,
            attempt=attempt,
        )
        fix_result = await send_task_to_worker(
            worker_id=worker_id,
            task_content=task_message,
            timeout_seconds=Timeouts.WORKER_SPAWN,
        )
        if fix_result.success:
            return True, worker_id
        if fix_result.error_message == "execution_timeout":
            # Worker likely dead, fall back to respawn
            logger.warning(
                "worker_reuse_failed_fallback",
                task_id=task_id,
                worker_id=worker_id,
            )
            success = await _respawn_developer_for_ci_fix(
                project=project,
                owner=owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                github_client=github_client,
                failure_context=failure_context,
                attempt=attempt,
            )
            return success, None  # worker_id invalidated
        # Worker alive but fix failed (agent error)
        return False, worker_id

    logger.info(
        "ci_fix_respawn_developer",
        task_id=task_id,
        attempt=attempt,
    )
    success = await _respawn_developer_for_ci_fix(
        project=project,
        owner=owner,
        repo_name=repo_name,
        repo_full_name=repo_full_name,
        github_client=github_client,
        failure_context=failure_context,
        attempt=attempt,
    )
    return success, None


async def _wait_for_ci_and_fix(
    project: dict,
    task_id: str,
    callback_stream: str | None,
    redis: RedisStreamClient,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
    worker_id: str | None = None,
    commit_sha: str | None = None,
) -> tuple[bool, list[dict]]:
    """Wait for CI workflow to pass, re-spawning developer on failure.

    After the developer pushes code, CI runs on GitHub. This function monitors
    the CI workflow and, if it fails, re-spawns a developer worker to fix
    the issues. Retries up to CI.MAX_FIX_RETRIES times.

    Returns:
        Tuple of (passed, ci_attempts) where ci_attempts is a list of dicts
        recording each CI attempt with status and failure context.
    """
    from shared.clients.github import GitHubAppClient, WorkflowNotFoundError

    from ..config.constants import CI

    ci_attempts: list[dict] = []

    repo_url = project.get("repository_url", "")
    if not repo_url or "github.com/" not in repo_url:
        logger.error("ci_check_fail_no_repo_url", task_id=task_id)
        return False, ci_attempts

    repo_full_name = repo_url.split("github.com/")[-1].rstrip("/")
    owner, repo_name = repo_full_name.split("/", 1)

    github_client = GitHubAppClient()

    # Initialize before loop: for attempt 0, use pre-developer timestamp so the
    # CI run created during development is visible to the filter.
    # Updated in the except block BEFORE respawning — after the failed run is
    # observed but before the new developer pushes (so the new CI run is visible).
    created_after = developer_started_at or datetime.now(UTC)

    gate_start = datetime.now(UTC)
    infra_rerun_attempted = False

    for attempt in range(CI.MAX_FIX_RETRIES + 1):  # 0 = initial check, 1..N = retries
        # Total gate timeout: abort if the entire CI fix loop has been running too long
        elapsed = (datetime.now(UTC) - gate_start).total_seconds()
        if elapsed > CI.TOTAL_GATE_TIMEOUT:
            logger.error(
                "ci_gate_total_timeout",
                task_id=task_id,
                elapsed_seconds=elapsed,
                timeout=CI.TOTAL_GATE_TIMEOUT,
            )
            ci_attempts.append({"attempt": attempt, "status": "gate_timeout"})
            await _record_ci_attempts(task_id, ci_attempts)
            return False, ci_attempts

        try:
            logger.info(
                "ci_check_waiting",
                task_id=task_id,
                attempt=attempt,
                workflow=CI.CI_WORKFLOW_FILE,
            )

            msg = "Waiting for CI checks..."
            if attempt > 0:
                msg = f"Waiting for CI checks (retry {attempt})..."
            await publish_callback_event(
                redis,
                callback_stream,
                "progress",
                task_id,
                msg,
                user_id=user_id,
                project_id=project.get("id", ""),
            )

            run_info = await github_client.wait_for_workflow_completion(
                owner=owner,
                repo=repo_name,
                workflow_file=CI.CI_WORKFLOW_FILE,
                branch="main",
                timeout_seconds=CI.WORKFLOW_TIMEOUT,
                poll_interval=CI.POLL_INTERVAL,
                created_after=created_after,
                head_sha=commit_sha,
            )

            logger.info(
                "ci_check_passed",
                task_id=task_id,
                attempt=attempt,
                run_id=run_info["id"],
            )
            ci_attempts.append({"attempt": attempt, "status": "passed"})
            await _record_ci_attempts(task_id, ci_attempts)
            return True, ci_attempts

        except WorkflowNotFoundError as e:
            # ci.yml doesn't exist — scaffold likely failed/skipped.
            # Fail-fast: no developer can fix a missing workflow file.
            logger.error(
                "ci_workflow_not_found",
                task_id=task_id,
                attempt=attempt,
                error=str(e),
            )
            ci_attempts.append({"attempt": attempt, "status": "workflow_not_found"})
            await _record_ci_attempts(task_id, ci_attempts)
            return False, ci_attempts

        except RuntimeError as e:
            # CI failed
            run_id = _extract_run_id_from_error(str(e))
            logger.warning(
                "ci_check_failed",
                task_id=task_id,
                attempt=attempt,
                error=str(e),
            )

            # Fetch failure details for context
            failure_context = ""
            if run_id:
                try:
                    failure_context = await github_client.get_workflow_failure_logs(
                        owner, repo_name, run_id
                    )
                except Exception as log_err:
                    logger.warning("ci_log_fetch_failed", error=str(log_err))

            ci_attempts.append(
                {
                    "attempt": attempt,
                    "status": "failed",
                    "failure_context": failure_context[:500] if failure_context else "",
                }
            )
            await _record_ci_attempts(task_id, ci_attempts)

            if attempt >= CI.MAX_FIX_RETRIES:
                logger.error(
                    "ci_fix_retries_exhausted",
                    task_id=task_id,
                    max_retries=CI.MAX_FIX_RETRIES,
                )
                return False, ci_attempts

            # Classify failure: infra issues can't be fixed by a developer
            if failure_context and _is_infra_failure(failure_context):
                if run_id and not infra_rerun_attempted:
                    infra_rerun_attempted = True
                    rerun_ok = await _try_infra_rerun(
                        github_client,
                        owner,
                        repo_name,
                        run_id,
                        task_id,
                        CI.WORKFLOW_TIMEOUT,
                        redis,
                        callback_stream,
                        user_id,
                        project.get("id", ""),
                    )
                    if rerun_ok is True:
                        ci_attempts.append({"attempt": attempt, "status": "passed_after_rerun"})
                        await _record_ci_attempts(task_id, ci_attempts)
                        return True, ci_attempts
                    if rerun_ok is False:
                        ci_attempts.append({"attempt": attempt, "status": "rerun_failed"})
                        await _record_ci_attempts(task_id, ci_attempts)
                        return False, ci_attempts
                    # rerun_ok is None → API error, fall through

                logger.error(
                    "ci_infra_failure",
                    task_id=task_id,
                    failure_context=failure_context,
                )
                return False, ci_attempts

            # Reset filters BEFORE fix: capture timestamp after the failed run
            # is observed (so it gets filtered out) but before the new push.
            # Clear commit_sha because the fix developer pushes a new commit
            # with a different SHA — fall back to created_after filtering.
            created_after, commit_sha = datetime.now(UTC), None

            fix_success, worker_id = await _attempt_developer_fix(
                worker_id=worker_id,
                project=project,
                owner=owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                github_client=github_client,
                failure_context=failure_context,
                attempt=attempt + 1,
                task_id=task_id,
            )

            if not fix_success:
                logger.error(
                    "ci_fix_developer_failed",
                    task_id=task_id,
                    attempt=attempt + 1,
                )
                return False, ci_attempts

            # Loop continues: will wait for CI again

        except TimeoutError:
            logger.error("ci_check_timeout", task_id=task_id, attempt=attempt)
            ci_attempts.append({"attempt": attempt, "status": "timeout"})
            await _record_ci_attempts(task_id, ci_attempts)
            return False, ci_attempts

    return False, ci_attempts


async def _record_ci_attempts(task_id: str, ci_attempts: list[dict]) -> None:
    """Persist CI attempts to task metadata via API."""
    try:
        await api_client.patch(
            f"tasks/{task_id}",
            json={"task_metadata": {"ci_attempts": ci_attempts}},
        )
    except Exception as e:
        logger.warning("ci_attempts_record_failed", task_id=task_id, error=str(e))


EXPECTED_REGISTRY_SECRETS_COUNT = 3


async def _create_repo_and_set_secrets(project: dict) -> None:
    """Create GitHub repo and set registry secrets for a draft project.

    Replaces the old scaffolder queue approach: repo creation and secret
    setup happen inline, while copier + make setup are deferred to
    worker-manager's scaffold phase.
    """
    from shared.clients.github import GitHubAppClient

    project_id = project["id"]
    project_name = project.get("name", project_id)

    org_name = os.getenv("GITHUB_ORG")
    if not org_name:
        raise RuntimeError("GITHUB_ORG environment variable is not set")

    # Generate repo name from project name
    repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
    repo_name = re.sub(r"[^a-z0-9-]", "", repo_name)
    repo_name = re.sub(r"-+", "-", repo_name).strip("-")
    if not repo_name:
        repo_name = project_id[:8]
    repo_full_name = f"{org_name}/{repo_name}"

    github_client = GitHubAppClient()

    # Step 1: Create repository (idempotent — handles "already exists")
    logger.info("creating_repo", org=org_name, repo=repo_name)
    try:
        await github_client.create_repo(
            org=org_name,
            name=repo_name,
            description=f"Project: {project_name}",
            private=True,
        )
        logger.info("repo_created", repo=repo_full_name)
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str or "422" in error_str:
            raise RuntimeError(
                f"Repository {repo_full_name} already exists. "
                "This likely means a previous run was not cleaned up. "
                "Delete the repo and retry, or use a different project name."
            ) from e
        else:
            raise

    # Step 2: Set registry secrets so CI can push Docker images
    registry_url = os.getenv("ORCHESTRATOR_HOSTNAME")
    registry_user = os.getenv("REGISTRY_USER")
    registry_password = os.getenv("REGISTRY_PASSWORD")

    if all([registry_url, registry_user, registry_password]):
        token = await github_client.get_org_token(org_name)
        count = await github_client.set_repository_secrets(
            org_name,
            repo_name,
            {
                "REGISTRY_URL": registry_url,
                "REGISTRY_USER": registry_user,
                "REGISTRY_PASSWORD": registry_password,
            },
            token=token,
        )
        if count < EXPECTED_REGISTRY_SECRETS_COUNT:
            logger.warning(
                "registry_secrets_incomplete",
                expected=EXPECTED_REGISTRY_SECRETS_COUNT,
                actual=count,
            )
    else:
        logger.warning(
            "registry_secrets_env_missing",
            has_url=bool(registry_url),
            has_user=bool(registry_user),
            has_password=bool(registry_password),
        )

    # Step 3: Update project status and repository URL
    repo_url = f"https://github.com/{repo_full_name}"
    await api_client.patch(
        f"projects/{project_id}",
        json={
            "status": ProjectStatus.SCAFFOLDING.value,
            "repository_url": repo_url,
        },
    )

    logger.info(
        "repo_created_and_secrets_set",
        project_id=project_id,
        repo_full_name=repo_full_name,
    )


async def process_engineering_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single engineering job by running Engineering Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    from ..subgraphs.engineering import create_engineering_subgraph

    task_id = job_data.get("task_id", "unknown")
    project_id = job_data.get("project_id")
    callback_stream = job_data.get("callback_stream")
    action = job_data.get("action", "create")
    description = job_data.get("description")
    skip_deploy = job_data.get("skip_deploy", False)
    user_id = job_data.get("user_id", "")

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        # Update task status to running
        await api_client.patch(
            f"tasks/{task_id}",
            json={"status": "running", "started_at": datetime.now(UTC).isoformat()},
        )

        # Publish progress event
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Engineering task started",
            user_id=user_id,
            project_id=project_id or "",
        )

        # Fetch project details (with user isolation)
        project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Fallback: use project config description when queue message has none
        if not description:
            description = (project.get("config") or {}).get("description", "")

        # Fail fast if scaffold previously failed
        project_status = project.get("status")
        if project_status == "scaffold_failed":
            error_msg = (
                f"Project {project_id} has status 'scaffold_failed'. "
                "Scaffold must succeed before developer can work. "
                "Fix the scaffolding issue and retry."
            )
            logger.error(
                "scaffold_failed_abort",
                task_id=task_id,
                project_id=project_id,
                action=action,
            )
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            await publish_callback_event(
                redis,
                callback_stream,
                "error",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            return {"status": "failed", "error": error_msg}

        # Create repo and set secrets for new project creation on draft projects
        if project_status == "draft" and action == "create":
            await _create_repo_and_set_secrets(project)
            # Refresh in-memory dict — _create_repo_and_set_secrets sets DB status
            # to "scaffolding". Developer node needs to see this to trigger copier.
            project["status"] = ProjectStatus.SCAFFOLDING.value
        elif project_status == "draft" and action != "create":
            logger.warning(
                "feature_fix_on_draft_project",
                task_id=task_id,
                project_id=project_id,
                action=action,
                hint="Project is in draft status but action is not 'create'. "
                "Skipping scaffolding — developer will work with existing repo.",
            )

        # Allocate resources if not already allocated
        existing_allocations = await api_client.get_project_allocations(project_id)
        if existing_allocations:
            # Convert existing allocations to allocated_resources format
            allocated_resources = {
                f"{a['server_handle']}:{a['port']}": a for a in existing_allocations
            }
            logger.info(
                "using_existing_allocations",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )
        else:
            # Run resource allocator to create allocations
            logger.info(
                "allocating_resources",
                task_id=task_id,
                project_id=project_id,
            )
            alloc_result = await resource_allocator_node.run(
                {
                    "project_id": project_id,
                    "project_spec": project,
                    "allocated_resources": {},
                    "errors": [],
                }
            )

            if alloc_result.get("errors"):
                error_msg = "; ".join(alloc_result["errors"])
                logger.error(
                    "resource_allocation_failed",
                    task_id=task_id,
                    project_id=project_id,
                    errors=alloc_result["errors"],
                )
                await api_client.patch(
                    f"tasks/{task_id}",
                    json={"status": "failed", "error_message": error_msg},
                )
                return {"status": "failed", "error": error_msg}

            allocated_resources = alloc_result.get("allocated_resources", {})
            logger.info(
                "resources_allocated",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )

        # Prepare EngineeringState
        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project,
            "allocated_resources": allocated_resources,
            "action": action,
            "description": description,
            "commit_sha": None,
            "worker_id": None,
            "engineering_status": "idle",
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "errors": [],
        }

        # NOTE: Do NOT update project status here.
        # Status must remain "scaffolding" so the developer node's
        # _build_scaffold_config() can detect it and trigger copier.
        # The developer node updates status to "scaffolded" after scaffold
        # succeeds, and engineering_worker sets "developing" after subgraph
        # completes.

        # Create and run engineering subgraph
        engineering_subgraph = create_engineering_subgraph()
        developer_started_at = datetime.now(UTC)
        result = await engineering_subgraph.ainvoke(subgraph_input)

        # Update project status to developing (after scaffold + code generation)
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEVELOPING.value},
        )

        # Check result status
        if result.get("engineering_status") == "done":
            logger.info(
                "engineering_job_success",
                task_id=task_id,
                commit_sha=result.get("commit_sha"),
            )

            # --- CI Gate & Auto-Deploy ---
            return await _handle_engineering_success(
                result=result,
                task_id=task_id,
                project=project,
                callback_stream=callback_stream,
                redis=redis,
                skip_deploy=skip_deploy,
                developer_started_at=developer_started_at,
                user_id=user_id,
            )

        elif result.get("engineering_status") == "blocked" or result.get("needs_human_approval"):
            logger.info("engineering_job_blocked", task_id=task_id, errors=result.get("errors"))
            await api_client.patch(
                f"tasks/{task_id}",
                json={
                    "status": "failed",
                    "error_message": "; ".join(result.get("errors", ["Task blocked"])),
                },
            )

            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                "Engineering task blocked or needs approval",
                user_id=user_id,
                project_id=project_id or "",
            )

            return {
                "status": "failed",
                "error": "; ".join(result.get("errors", ["Task blocked"])),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        else:
            # Unknown status
            errors = result.get("errors", ["Unknown engineering status"])
            logger.error("engineering_job_unknown_status", task_id=task_id, errors=errors)
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": "; ".join(errors)},
            )
            return {
                "status": "failed",
                "error": "; ".join(errors),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await api_client.patch(
            f"tasks/{task_id}",
            json={"status": "failed", "error_message": str(e), "error_traceback": str(e)},
        )
        # Update project status to failed
        if project_id:
            await api_client.patch(
                f"projects/{project_id}",
                json={"status": ProjectStatus.FAILED.value},
            )

        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            f"Engineering task failed: {e!s}",
            user_id=user_id,
            project_id=project_id or "",
        )

        return {
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(UTC).isoformat(),
        }


async def _handle_engineering_success(
    result: dict,
    task_id: str,
    project: dict,
    callback_stream: str | None,
    redis: RedisStreamClient,
    skip_deploy: bool,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
) -> dict:
    """Handle successful engineering result: CI gate and auto-deploy."""
    project_id = project["id"]

    # --- commit_sha gate: fail fast if no code was committed ---
    if not result.get("commit_sha"):
        logger.error("no_commit_sha", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"tasks/{task_id}",
            json={
                "status": "failed",
                "error_message": "Developer completed but no commit was made",
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            "Development completed but no code was committed",
            user_id=user_id,
            project_id=project_id,
        )
        return {
            "status": "failed",
            "error": "No commit_sha",
            "finished_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "engineering_job_success",
        task_id=task_id,
        commit_sha=result.get("commit_sha"),
    )

    # --- Refresh project before CI check (repo_url may have been updated) ---
    fresh_project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
    if fresh_project:
        project = fresh_project

    # --- CI Gate: wait for ci.yml before triggering deploy ---
    worker_id = result.get("worker_id")
    try:
        ci_passed, ci_attempts = await _wait_for_ci_and_fix(
            project=project,
            task_id=task_id,
            callback_stream=callback_stream,
            redis=redis,
            developer_started_at=developer_started_at,
            user_id=user_id,
            worker_id=worker_id,
            commit_sha=result.get("commit_sha"),
        )
    finally:
        # Cleanup: delete worker container after CI gate (regardless of outcome)
        if worker_id:
            try:
                await delete_worker(worker_id)
                logger.info("worker_deleted_after_ci_gate", worker_id=worker_id)
            except Exception as e:
                logger.warning("worker_delete_failed", worker_id=worker_id, error=str(e))

    failed_count = sum(1 for a in ci_attempts if a["status"] == "failed")

    if not ci_passed:
        fail_msg = f"CI failed after {len(ci_attempts)} attempt(s), retries exhausted"
        logger.error("ci_gate_failed", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"tasks/{task_id}",
            json={
                "status": "failed",
                "error_message": fail_msg,
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            fail_msg,
            user_id=user_id,
            project_id=project_id,
        )
        return {
            "status": "failed",
            "error": fail_msg,
            "finished_at": datetime.now(UTC).isoformat(),
        }

    # CI passed — mark engineering task as completed
    await api_client.patch(
        f"tasks/{task_id}",
        json={
            "status": "completed",
            "result": {
                "engineering_status": result["engineering_status"],
                "commit_sha": result.get("commit_sha"),
                "selected_modules": result.get("selected_modules"),
                "test_results": result.get("test_results"),
            },
        },
    )

    ci_summary = "CI passed"
    if failed_count:
        ci_summary = f"CI passed after {failed_count} failed attempt(s)"

    if skip_deploy:
        # This IS the final step — tell user we're done
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            f"Engineering task completed, {ci_summary}",
            user_id=user_id,
            project_id=project_id,
        )
    else:
        # Deploy is next — only send progress, deploy worker sends "completed" on success
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            f"{ci_summary}, deploying...",
            user_id=user_id,
            project_id=project_id,
        )

    # Auto-trigger deploy after CI passes (unless skip_deploy)
    if not skip_deploy:
        deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
        try:
            # Create deploy task in API
            await api_client.post(
                "tasks/",
                json={
                    "id": deploy_task_id,
                    "type": TaskType.DEPLOY.value,
                    "project_id": project_id,
                    "status": TaskStatus.QUEUED.value,
                },
            )
            # Queue deploy job
            deploy_msg = DeployMessage(
                task_id=deploy_task_id,
                project_id=project_id,
                user_id=user_id,
                callback_stream=callback_stream,
                triggered_by=DeployTrigger.ENGINEERING,
            )
            await redis.redis.xadd(
                DEPLOY_QUEUE,
                {"data": deploy_msg.model_dump_json()},
            )
            logger.info(
                "deploy_auto_triggered",
                task_id=task_id,
                deploy_task_id=deploy_task_id,
                project_id=project_id,
            )
        except Exception as e:
            logger.error(
                "deploy_auto_trigger_failed",
                task_id=task_id,
                error=str(e),
            )
            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                f"CI passed but deploy trigger failed: {e}",
                user_id=user_id,
                project_id=project_id,
            )
    else:
        deploy_task_id = None
        logger.info(
            "deploy_skipped",
            task_id=task_id,
            project_id=project_id,
        )

    return {
        "status": "success",
        "commit_sha": result.get("commit_sha"),
        "deploy_task_id": deploy_task_id,
        "finished_at": datetime.now(UTC).isoformat(),
    }


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="engineering-worker",
        queue=ENGINEERING_QUEUE,
        process_fn=process_engineering_job,
    )


if __name__ == "__main__":
    main()
