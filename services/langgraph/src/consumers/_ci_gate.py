"""CI gate — wait for CI workflow, classify failures, retry with developer fixes.

Extracted from engineering_worker.py (#18).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from shared.clients.github import GitHubAppClient

from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.worker_spawner import send_task_to_worker
from ._events import publish_callback_event

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


def _build_ci_fix_prompt(failure_context: str, attempt: int, run_url: str | None = None) -> str:
    """Build the prompt for CI fix task (used by both reuse and respawn paths).

    Includes reject instructions so the worker can signal when a failure
    is not a code issue (infrastructure, missing secrets, orchestrator bug).
    """
    run_info = ""
    if run_url:
        run_info = f"\n**Run URL**: {run_url}\n"

    return f"""# Task: Fix CI Failures (Attempt {attempt})

## CI Failure Details
{run_info}
{failure_context or "CI workflow failed. Check the CI logs for details."}

## Instructions

1. The repository is already cloned to `/workspace`. Pull latest changes with `git pull`.
2. Analyze the CI failure details above to understand the root cause.
   - Use `gh run list --branch main` and `gh run view <run-id> --log` for full CI logs.
3. Determine if this is a code issue you can fix, or an infrastructure/config problem.
4. **If you CAN fix it**: fix the root cause, run local checks, commit and push.
5. **If this is NOT a code issue** (infrastructure, missing secrets, orchestrator bug,
   registry auth, Docker config, etc.): do NOT make any commits. Instead, write a
   `## REJECTED` section in your response explaining:
   - What failed and why
   - Why you cannot fix it (e.g. "REGISTRY_PASSWORD secret is empty")
   - Suggested action for the admin

## Important

- Focus ONLY on fixing the CI failures, do not add new features.
- Make a descriptive commit message explaining what you fixed.
- If the problem is outside your control, use ## REJECTED — do not make empty commits.
"""


async def _respawn_developer_for_ci_fix(
    project: dict,
    owner: str,
    repo_name: str,
    repo_full_name: str,
    github_client: GitHubAppClient,
    failure_context: str,
    attempt: int,
    run_url: str | None = None,
) -> tuple[bool, str | None]:
    """Spawn a new developer worker to fix CI failures.

    Returns:
        Tuple of (success, reject_reason). reject_reason is set when worker
        signals that the failure is not a code issue.
    """
    from ..clients.worker_spawner import request_spawn
    from ..config.constants import Timeouts

    access_token = await github_client.get_token(owner, repo_name)
    project_name = project.get("name", "project")
    task_message = _build_ci_fix_prompt(failure_context, attempt, run_url=run_url)

    worker_result = await request_spawn(
        repo=repo_full_name,
        github_token=access_token,
        task_content=task_message,
        task_title=f"Fix CI failures for {project_name} (attempt {attempt})",
        timeout_seconds=Timeouts.WORKER_SPAWN,
        project_id=project.get("id"),
    )

    return worker_result.success, worker_result.reject_reason


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
    run_url: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Try to fix a CI code failure via an existing worker or by respawning.

    Returns:
        Tuple of (success, updated_worker_id, reject_reason).
        worker_id may become None if the existing worker timed out.
        reject_reason is set when worker signals failure is not a code issue.
    """
    from ..config.constants import Timeouts

    task_message = _build_ci_fix_prompt(failure_context, attempt, run_url=run_url)

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
            return True, worker_id, None
        if fix_result.reject_reason:
            return False, worker_id, fix_result.reject_reason
        if fix_result.error_message == "execution_timeout":
            # Worker likely dead, fall back to respawn
            logger.warning(
                "worker_reuse_failed_fallback",
                task_id=task_id,
                worker_id=worker_id,
            )
            success, reject_reason = await _respawn_developer_for_ci_fix(
                project=project,
                owner=owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                github_client=github_client,
                failure_context=failure_context,
                attempt=attempt,
                run_url=run_url,
            )
            return success, None, reject_reason  # worker_id invalidated
        # Worker alive but fix failed (agent error)
        return False, worker_id, None

    logger.info(
        "ci_fix_respawn_developer",
        task_id=task_id,
        attempt=attempt,
    )
    success, reject_reason = await _respawn_developer_for_ci_fix(
        project=project,
        owner=owner,
        repo_name=repo_name,
        repo_full_name=repo_full_name,
        github_client=github_client,
        failure_context=failure_context,
        attempt=attempt,
        run_url=run_url,
    )
    return success, None, reject_reason


async def _fetch_failure_context(
    github_client: GitHubAppClient, owner: str, repo_name: str, run_id: int | None
) -> str:
    """Fetch CI failure logs from GitHub. Returns empty string on failure."""
    if not run_id:
        return ""
    try:
        return await github_client.get_workflow_failure_logs(owner, repo_name, run_id)
    except Exception as e:
        logger.warning("ci_log_fetch_failed", error=str(e))
        return ""


def _build_run_url(repo_full_name: str, run_id: int | None) -> str | None:
    """Build GitHub Actions run URL from repo name and run ID."""
    if not run_id:
        return None
    return f"https://github.com/{repo_full_name}/actions/runs/{run_id}"


async def _wait_for_ci_and_fix(
    project: dict,
    git_url: str,
    task_id: str,
    callback_stream: str | None,
    redis: RedisStreamClient,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
    worker_id: str | None = None,
    commit_sha: str | None = None,
) -> tuple[bool, list[dict], bool, str | None]:
    """Wait for CI workflow to pass, re-spawning developer on failure.

    After the developer pushes code, CI runs on GitHub. This function monitors
    the CI workflow and, if it fails, re-spawns a developer worker to fix
    the issues. Retries up to CI.MAX_FIX_RETRIES times.

    Returns:
        Tuple of (passed, ci_attempts, rejected, reject_reason).
        rejected is True when a worker determined the failure is not a code issue.
        reject_reason explains why (infrastructure, missing secrets, etc.).
    """
    from shared.clients.github import GitHubAppClient, WorkflowNotFoundError

    from ..config.constants import CI

    ci_attempts: list[dict] = []

    if not git_url or "github.com/" not in git_url:
        logger.error("ci_check_fail_no_repo_url", task_id=task_id)
        return False, ci_attempts, False, None

    repo_full_name = git_url.split("github.com/")[-1].rstrip("/").removesuffix(".git")
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
            return False, ci_attempts, False, None

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
            return True, ci_attempts, False, None

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
            return False, ci_attempts, False, None

        except RuntimeError as e:
            # CI failed
            run_id = _extract_run_id_from_error(str(e))
            logger.warning(
                "ci_check_failed",
                task_id=task_id,
                attempt=attempt,
                error=str(e),
            )

            failure_context = await _fetch_failure_context(github_client, owner, repo_name, run_id)

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
                return False, ci_attempts, False, None

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
                        return True, ci_attempts, False, None
                    if rerun_ok is False:
                        ci_attempts.append({"attempt": attempt, "status": "rerun_failed"})
                        await _record_ci_attempts(task_id, ci_attempts)
                        return False, ci_attempts, False, None

                logger.error(
                    "ci_infra_failure",
                    task_id=task_id,
                    failure_context=failure_context,
                )
                return False, ci_attempts, False, None

            # Reset filters BEFORE fix: capture timestamp after the failed run
            # is observed (so it gets filtered out) but before the new push.
            # Clear commit_sha because the fix developer pushes a new commit
            # with a different SHA — fall back to created_after filtering.
            created_after, commit_sha = datetime.now(UTC), None

            fix_success, worker_id, reject_reason = await _attempt_developer_fix(
                worker_id=worker_id,
                project=project,
                owner=owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                github_client=github_client,
                failure_context=failure_context,
                attempt=attempt + 1,
                task_id=task_id,
                run_url=_build_run_url(repo_full_name, run_id),
            )

            if reject_reason:
                logger.warning(
                    "ci_fix_worker_rejected",
                    task_id=task_id,
                    reject_reason=reject_reason[:200],
                )
                ci_attempts.append(
                    {"attempt": attempt, "status": "rejected", "reject_reason": reject_reason}
                )
                await _record_ci_attempts(task_id, ci_attempts)
                return False, ci_attempts, True, reject_reason

            if not fix_success:
                logger.error(
                    "ci_fix_developer_failed",
                    task_id=task_id,
                    attempt=attempt + 1,
                )
                return False, ci_attempts, False, None

            # Loop continues: will wait for CI again

        except TimeoutError:
            logger.error("ci_check_timeout", task_id=task_id, attempt=attempt)
            ci_attempts.append({"attempt": attempt, "status": "timeout"})
            await _record_ci_attempts(task_id, ci_attempts)
            return False, ci_attempts, False, None

    return False, ci_attempts, False, None


async def _record_ci_attempts(task_id: str, ci_attempts: list[dict]) -> None:
    """Persist CI attempts to task metadata via API."""
    try:
        await api_client.patch(
            f"runs/{task_id}",
            json={"task_metadata": {"ci_attempts": ci_attempts}},
        )
    except Exception as e:
        logger.warning("ci_attempts_record_failed", task_id=task_id, error=str(e))
