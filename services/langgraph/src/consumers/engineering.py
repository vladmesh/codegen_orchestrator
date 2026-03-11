"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.consumers.engineering
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.notifications import notify_admins
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import get_story_worker, set_story_worker
from ..clients.worker_spawner import delete_worker
from ..nodes.resource_allocator import resource_allocator_node
from ._base import start_worker
from ._ci_gate import _wait_for_ci_and_fix
from ._events import publish_callback_event
from ._repo_setup import _create_repo_and_set_secrets


def _parse_telegram_id(user_id: str) -> dict:
    """Build get_project kwargs with telegram_id if user_id is numeric."""
    if user_id and user_id.isdigit():
        return {"telegram_id": int(user_id)}
    return {}


logger = structlog.get_logger(__name__)


async def _update_task_status(
    api, planning_task_id: str, status: str, actor: str = "engineering-worker"
) -> None:
    """Transition a planning-layer task to the given status (best-effort).

    The task state machine requires intermediate steps (in_dev → in_ci → testing → done).
    This helper walks through them automatically when the target is 'done'.
    """
    # For "done", walk through the intermediate states the state machine requires
    if status == "done":
        steps = ["in_ci", "testing", "done"]
    else:
        steps = [status]

    for step in steps:
        try:
            await api.post(
                f"tasks/{planning_task_id}/transition",
                params={"to_status": step},
                json={"actor": actor},
            )
            logger.info(
                "task_status_updated",
                planning_task_id=planning_task_id,
                new_status=step,
            )
        except Exception:
            logger.warning(
                "task_status_update_failed",
                planning_task_id=planning_task_id,
                target_status=step,
                exc_info=True,
            )
            break


async def _write_task_event(api, planning_task_id: str, event_type: str, details: dict) -> None:
    """Write an event to a planning-layer task (best-effort)."""
    try:
        await api.post(
            f"tasks/{planning_task_id}/events",
            json={
                "event_type": event_type,
                "details": details,
                "actor": "engineering-worker",
            },
        )
    except Exception:
        logger.warning(
            "task_event_write_failed",
            planning_task_id=planning_task_id,
            event_type=event_type,
            exc_info=True,
        )


async def _build_story_context(story_id: str, current_task_id: str | None = None) -> str | None:
    """Build a summary of previous story tasks with their events.

    Returns a formatted string for inclusion in the worker's task message,
    giving continuity context so the worker doesn't re-gather information.
    Returns None if story has no tasks or fetch fails.
    """
    try:
        tasks = await api_client.get_tasks_by_story(story_id)
    except Exception:
        logger.warning("story_context_fetch_failed", story_id=story_id, exc_info=True)
        return None

    if not tasks:
        return None

    # Sort by created_at for chronological order
    tasks.sort(key=lambda t: t.get("created_at", ""))

    lines: list[str] = []
    for task in tasks:
        tid = task.get("id", "?")
        title = task.get("title", "Untitled")
        status = task.get("status", "unknown")
        is_current = tid == current_task_id
        marker = " ← CURRENT" if is_current else ""
        lines.append(f"### Task: {title} [{status}]{marker}")

        if task.get("description"):
            lines.append(f"Description: {task['description'][:300]}")

        # Fetch events for this task
        try:
            events = await api_client.get_task_events(tid)
        except Exception:
            events = []

        if events:
            lines.append("Events:")
            for ev in events:
                etype = ev.get("event_type", "?")
                actor = ev.get("actor", "?")
                details = ev.get("details") or {}
                detail_str = ""
                if etype == "status_change":
                    detail_str = f"{ev.get('from_status')} → {ev.get('to_status')}"
                elif details:
                    # Compact summary of details
                    parts = [f"{k}={v}" for k, v in list(details.items())[:5]]
                    detail_str = ", ".join(parts)
                lines.append(f"  - [{etype}] {detail_str} (by {actor})")

        lines.append("")

    return "\n".join(lines)


async def _resolve_allocations(task_id: str, project_id: str, project: dict) -> dict | None:
    """Resolve or create resource allocations. Returns dict or None on failure."""
    existing = await api_client.get_project_allocations(project_id)
    if existing:
        allocated = {f"{a['server_handle']}:{a['port']}": a for a in existing}
        logger.info("using_existing_allocations", task_id=task_id, count=len(allocated))
        return allocated

    logger.info("allocating_resources", task_id=task_id, project_id=project_id)
    result = await resource_allocator_node.run(
        {"project_id": project_id, "project_spec": project, "allocated_resources": {}, "errors": []}
    )
    if result.get("errors"):
        error_msg = "; ".join(result["errors"])
        logger.error("resource_allocation_failed", task_id=task_id, errors=result["errors"])
        await api_client.patch(
            f"runs/{task_id}", json={"status": "failed", "error_message": error_msg}
        )
        return None

    allocated = result.get("allocated_resources", {})
    logger.info("resources_allocated", task_id=task_id, count=len(allocated))
    return allocated


async def _handle_worker_reject(
    task_id: str,
    project_id: str,
    planning_task_id: str | None,
    story_id: str | None,
    reject_reason: str,
    ci_attempts: list[dict],
) -> dict:
    """Handle worker reject: task → blocked, story → failed, admin notified.

    Worker reject means the CI failure is not a code issue (infrastructure,
    missing secrets, orchestrator bug). The pipeline halts and an admin
    is notified to investigate.
    """
    logger.warning(
        "worker_rejected_ci_fix",
        task_id=task_id,
        project_id=project_id,
        reject_reason=reject_reason[:200],
    )

    # Mark the engineering run as failed
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "failed",
            "error_message": f"Worker rejected: {reject_reason[:500]}",
        },
    )

    # Planning task → failed with reject metadata (supervisor skips worker_rejected)
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, "failed")
        try:
            await api_client.patch(
                f"tasks/{planning_task_id}",
                json={
                    "failure_metadata": {
                        "failure_reason": "worker_rejected",
                        "reject_reason": reject_reason,
                    },
                },
            )
        except Exception:
            logger.warning(
                "task_failure_metadata_write_failed",
                planning_task_id=planning_task_id,
                exc_info=True,
            )
        await _write_task_event(
            api_client,
            planning_task_id,
            "note",
            {
                "action": "worker_rejected",
                "reject_reason": reject_reason,
                "ci_attempts": len(ci_attempts),
            },
        )

    # Story → failed with reject metadata
    if story_id:
        try:
            await api_client.patch(
                f"stories/{story_id}",
                json={
                    "status": "failed",
                    "failure_metadata": {
                        "failure_reason": "worker_rejected",
                        "reject_reason": reject_reason,
                        "task_id": task_id,
                        "planning_task_id": planning_task_id,
                    },
                },
            )
        except Exception:
            logger.warning("story_fail_on_reject_failed", story_id=story_id, exc_info=True)

    # Notify admin (not PO, not user — this is an orchestrator issue)
    try:
        await notify_admins(
            f"Worker rejected CI fix for task {task_id} (project {project_id}):\n{reject_reason}",
            level="error",
        )
    except Exception:
        logger.warning("admin_notify_on_reject_failed", task_id=task_id, exc_info=True)

    return {
        "status": "failed",
        "rejected": True,
        "reject_reason": reject_reason,
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _fail_job(task_id: str, error_msg: str, planning_task_id: str | None = None) -> dict:
    """Mark a run as failed and optionally update planning task."""
    await api_client.patch(f"runs/{task_id}", json={"status": "failed", "error_message": error_msg})
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, "failed")
    return {"status": "failed", "error": error_msg}


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
    planning_task_id = job_data.get("planning_task_id")
    story_id = job_data.get("story_id")

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        # Update task status to running
        await api_client.patch(
            f"runs/{task_id}",
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
            return await _fail_job(task_id, f"Project {project_id} not found", planning_task_id)

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
            await publish_callback_event(
                redis,
                callback_stream,
                "error",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            return await _fail_job(task_id, error_msg, planning_task_id)

        # Create repo and set secrets for new project creation on draft projects
        if project_status == "draft" and action == "create":
            await _create_repo_and_set_secrets(project)
            # Refresh in-memory dict — _create_repo_and_set_secrets sets DB status
            # to "scaffolding". Scaffolder service picks up from here.
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
        allocated_resources = await _resolve_allocations(task_id, project_id, project)
        if allocated_resources is None:
            return {"status": "failed", "error": "Resource allocation failed"}

        # Look up existing worker for story-level reuse
        existing_worker_id = None
        if story_id:
            existing_worker_id = await get_story_worker(redis.redis, story_id)
            if existing_worker_id:
                logger.info(
                    "reusing_story_worker",
                    story_id=story_id,
                    worker_id=existing_worker_id,
                    task_id=task_id,
                )

        # Fetch repo_id for workspace mounting
        repo_id = (await api_client.get_primary_repository(project_id) or {}).get("id")

        # Build story context (previous tasks + events) for worker continuity
        story_context = await _build_story_context(story_id, planning_task_id) if story_id else None

        # Prepare EngineeringState
        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project,
            "allocated_resources": allocated_resources,
            "action": action,
            "description": description,
            "story_context": story_context,
            "repo_id": repo_id,
            "commit_sha": None,
            "worker_id": existing_worker_id,
            "engineering_status": "idle",
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "errors": [],
        }

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
                action=action,
                planning_task_id=planning_task_id,
                story_id=story_id,
            )

        else:
            # Blocked, needs approval, or unknown status
            errors = result.get("errors", ["Unknown engineering status"])
            error_msg = "; ".join(errors)
            logger.error("engineering_job_failed_status", task_id=task_id, errors=errors)
            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            return await _fail_job(task_id, error_msg, planning_task_id)

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
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
        return await _fail_job(task_id, str(e), planning_task_id)


async def _should_run_ci_gate(planning_task_id: str | None) -> bool:
    """Determine whether CI gate should run for this task.

    CI gate runs for:
    - Standalone tasks (no planning_task_id)
    - CI check tasks (created_by=system, appended by architect)

    Ordinary story tasks skip CI gate — CI runs once at story end.
    """
    if not planning_task_id:
        return True
    try:
        planning_task = await api_client.get(f"tasks/{planning_task_id}")
        return planning_task.get("created_by") == "system"
    except Exception:
        logger.warning(
            "planning_task_fetch_failed",
            planning_task_id=planning_task_id,
            exc_info=True,
        )
        return False


async def _run_ci_gate_and_handle_failure(  # noqa: PLR0913
    project: dict,
    task_id: str,
    callback_stream: str | None,
    redis: RedisStreamClient,
    developer_started_at: datetime | None,
    user_id: str,
    worker_id: str | None,
    commit_sha: str | None,
    planning_task_id: str | None,
    story_id: str | None,
) -> dict | None:
    """Run CI gate, handle failures. Returns failure dict or None if CI passed."""
    project_id = project["id"]

    # Refresh project before CI check
    fresh_project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
    if fresh_project:
        project = fresh_project

    primary_repo = await api_client.get_primary_repository(project_id)
    git_url = primary_repo.get("git_url", "") if primary_repo else ""

    try:
        ci_passed, ci_attempts, ci_rejected, ci_reject_reason = await _wait_for_ci_and_fix(
            project=project,
            git_url=git_url,
            task_id=task_id,
            callback_stream=callback_stream,
            redis=redis,
            developer_started_at=developer_started_at,
            user_id=user_id,
            worker_id=worker_id,
            commit_sha=commit_sha,
        )
    finally:
        # Worker lifecycle: keep alive for story reuse, or delete for standalone
        if worker_id:
            if story_id:
                try:
                    await set_story_worker(redis.redis, story_id, worker_id)
                except Exception as e:
                    logger.warning(
                        "story_worker_register_failed",
                        worker_id=worker_id,
                        story_id=story_id,
                        error=str(e),
                    )
            else:
                try:
                    await delete_worker(worker_id, reason="completed")
                    logger.info("worker_deleted_after_ci_gate", worker_id=worker_id)
                except Exception as e:
                    logger.warning("worker_delete_failed", worker_id=worker_id, error=str(e))

    if ci_rejected and ci_reject_reason:
        return await _handle_worker_reject(
            task_id=task_id,
            project_id=project_id,
            planning_task_id=planning_task_id,
            story_id=story_id,
            reject_reason=ci_reject_reason,
            ci_attempts=ci_attempts,
        )

    if not ci_passed:
        fail_msg = f"CI failed after {len(ci_attempts)} attempt(s), retries exhausted"
        logger.error("ci_gate_failed", task_id=task_id, project_id=project_id)
        if planning_task_id:
            await _update_task_status(api_client, planning_task_id, "failed")
        await api_client.patch(
            f"runs/{task_id}",
            json={"status": "failed", "error_message": fail_msg},
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
        return {"status": "failed", "error": fail_msg, "finished_at": datetime.now(UTC).isoformat()}

    return None  # CI passed


async def _handle_engineering_success(  # noqa: PLR0913
    result: dict,
    task_id: str,
    project: dict,
    callback_stream: str | None,
    redis: RedisStreamClient,
    skip_deploy: bool,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
    action: str = "create",
    planning_task_id: str | None = None,
    story_id: str | None = None,
) -> dict:
    """Handle successful engineering result: CI gate and auto-deploy."""
    project_id = project["id"]

    # --- commit_sha gate: fail fast if no code was committed ---
    if not result.get("commit_sha"):
        logger.error("no_commit_sha", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"runs/{task_id}",
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

    logger.info("engineering_job_success", task_id=task_id, commit_sha=result.get("commit_sha"))

    run_ci_gate = await _should_run_ci_gate(planning_task_id)
    logger.info(
        "ci_gate_decision",
        task_id=task_id,
        planning_task_id=planning_task_id,
        run_ci_gate=run_ci_gate,
    )

    worker_id = result.get("worker_id")

    if run_ci_gate:
        failure = await _run_ci_gate_and_handle_failure(
            project=project,
            task_id=task_id,
            callback_stream=callback_stream,
            redis=redis,
            developer_started_at=developer_started_at,
            user_id=user_id,
            worker_id=worker_id,
            commit_sha=result.get("commit_sha"),
            planning_task_id=planning_task_id,
            story_id=story_id,
        )
        if failure:
            return failure
    elif worker_id and story_id:
        # Ordinary story task: skip CI gate, register worker for reuse
        try:
            await set_story_worker(redis.redis, story_id, worker_id)
        except Exception as e:
            logger.warning(
                "story_worker_register_failed",
                worker_id=worker_id,
                story_id=story_id,
                error=str(e),
            )

    # CI passed (or skipped) — mark engineering task as completed
    await api_client.patch(
        f"runs/{task_id}",
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

    # Update planning-layer task if linked
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, "done")
        await _write_task_event(
            api_client,
            planning_task_id,
            "iteration_end",
            {
                "commit_sha": result.get("commit_sha"),
                "ci_result": "passed",
                "summary": f"Engineering run {task_id} completed",
            },
        )

    ci_summary = "Task completed (CI deferred)" if not run_ci_gate else "CI passed"

    # When planning_task_id is set, skip deploy (dispatcher handles it on story complete)
    effective_skip_deploy = skip_deploy or bool(planning_task_id)

    logger.info(
        "deploy_decision",
        task_id=task_id,
        planning_task_id=planning_task_id,
        skip_deploy=skip_deploy,
        effective_skip_deploy=effective_skip_deploy,
    )

    if effective_skip_deploy:
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

    # Auto-trigger deploy after CI passes (unless skip_deploy or task-linked)
    if not effective_skip_deploy:
        deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
        try:
            # Create deploy task in API
            await api_client.post(
                "runs/",
                json={
                    "id": deploy_task_id,
                    "type": RunType.DEPLOY.value,
                    "project_id": project_id,
                    "status": RunStatus.QUEUED.value,
                },
            )
            # Queue deploy job
            deploy_msg = DeployMessage(
                task_id=deploy_task_id,
                project_id=project_id,
                user_id=user_id,
                callback_stream=callback_stream,
                triggered_by=DeployTrigger.ENGINEERING,
                action=action,
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
