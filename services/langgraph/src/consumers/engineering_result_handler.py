"""Result handlers for engineering worker outcomes (success, gave_up, technical failure)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.worker_result import WorkerStopReason
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import set_story_worker
from ..clients.worker_spawner import delete_worker
from ._events import publish_callback_event, publish_story_event
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)


@dataclass
class EngineeringSuccessParams:
    """Parameters for handle_engineering_success."""

    result: dict
    task_id: str
    project: ProjectDTO
    callback_stream: str | None
    redis: RedisStreamClient
    skip_deploy: bool
    developer_started_at: datetime | None = None
    telegram_chat_id: str = ""
    action: str = "create"
    planning_task_id: str | None = None
    story_id: str | None = None
    deploy_fix_attempt: int = 0
    worker_observability: dict | None = None


def _observability_patch(worker_observability: dict | None) -> dict:
    """Keep compatibility artifacts on Run and canonical usage in the ledger.

    Claude evidence already carries a Decimal-derived integer micro-USD value
    from its one final-result object. Other agents retain available facts with
    explicit unknown cost; the mutable Run float is never a cost source.
    """
    observability = worker_observability or {}
    claude_evidence = observability.get("claude_evidence")
    if claude_evidence is not None:
        attempt = {"claude_evidence": claude_evidence}
    else:
        profile = observability.get("agent_profile") or {}
        attempt = {
            "provider": profile.get("provider"),
            "model": profile.get("model"),
            "input_tokens": observability.get("input_tokens"),
            "output_tokens": observability.get("output_tokens"),
            "total_tokens": observability.get("total_tokens"),
            "cache_read_tokens": observability.get("cache_read_tokens"),
            "cache_write_tokens": observability.get("cache_write_tokens"),
            "cost_source": "unknown",
        }
    return {
        "engineering_attempt": attempt,
        **{
            field: observability.get(field)
            for field in ("transcript_path", "transcript_truncated", "agent_profile")
            if observability.get(field) is not None
        },
    }


async def _update_task_status(
    api, planning_task_id: str, status: str, actor: str = "engineering-worker"
) -> None:
    """Transition a planning-layer task to the given status (best-effort)."""
    if status == TaskStatus.DONE:
        steps = [TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE]
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


def _stop_patch(stop_reason: WorkerStopReason | None, agent_limit_seconds: int | None) -> dict:
    """`run_metadata` naming why a turn stopped, or nothing when it did not.

    The API merges `run_metadata`, so an absent stop reason leaves the attempt's
    existing metadata — including the worker and limit recorded at spawn —
    untouched instead of blanking it.
    """
    if stop_reason is None:
        return {}
    metadata = {"stop_reason": stop_reason.value}
    if agent_limit_seconds is not None:
        metadata["agent_limit_seconds"] = agent_limit_seconds
    return {"run_metadata": metadata}


async def fail_job(
    task_id: str,
    error_msg: str,
    planning_task_id: str | None = None,
    worker_observability: dict | None = None,
    stop_reason: WorkerStopReason | None = None,
    agent_limit_seconds: int | None = None,
) -> dict:
    """Mark a run as failed and optionally update planning task."""
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": EngineeringRunResult(engineering_status=EngineeringStatus.FAILED).model_dump(
                mode="json"
            ),
            **_observability_patch(worker_observability),
            **_stop_patch(stop_reason, agent_limit_seconds),
        },
    )
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, TaskStatus.FAILED)
    return live_work_unsettled({"status": "failed", "error": error_msg})


async def handle_worker_gave_up(
    task_id: str,
    project_id: str,
    planning_task_id: str | None,
    story_id: str | None,
    reason: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    worker_observability: dict | None = None,
) -> dict:
    """Handle worker gave_up: task/story → WHR, admin notified, user informed.

    Covers both former "blocked" (worker hit a blocker) and "rejected" (infra issue)
    paths — in both cases the worker explicitly could not complete the task and
    a human needs to intervene.
    """
    logger.warning(
        "worker_gave_up",
        task_id=task_id,
        project_id=project_id,
        reason=reason[:200],
    )

    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": f"Worker gave up: {reason[:500]}",
            "result": EngineeringRunResult(engineering_status=EngineeringStatus.GAVE_UP).model_dump(
                mode="json"
            ),
            **_observability_patch(worker_observability),
            # A refusal is a stop with a reason, and it is the third one the
            # attempt can carry: the agent declined rather than ran out of time
            # or went quiet.
            **_stop_patch(WorkerStopReason.AGENT_REFUSED, None),
        },
    )

    if planning_task_id:
        try:
            await api_client.post(
                f"tasks/{planning_task_id}/transition",
                params={"to_status": TaskStatus.WAITING_HUMAN_REVIEW.value},
                json={"actor": "engineering-worker"},
            )
        except Exception:
            logger.warning(
                "task_whr_transition_failed",
                planning_task_id=planning_task_id,
                exc_info=True,
            )
        try:
            await api_client.patch(
                f"tasks/{planning_task_id}",
                json={
                    "failure_metadata": {"reason": reason},
                },
            )
        except Exception:
            logger.warning(
                "task_gave_up_metadata_write_failed",
                planning_task_id=planning_task_id,
                exc_info=True,
            )
        await _write_task_event(
            api_client,
            planning_task_id,
            "note",
            {"action": "worker_gave_up", "reason": reason},
        )

    if story_id:
        try:
            await api_client.patch(
                f"stories/{story_id}",
                json={"status": StoryStatus.WAITING_HUMAN_REVIEW.value},
            )
        except Exception:
            logger.warning("story_whr_on_gave_up_failed", story_id=story_id, exc_info=True)

    await notify_admins_best_effort(
        f"Worker gave up on task {planning_task_id or task_id} (project {project_id}):\n{reason}",
        level="warning",
        component="engineering_result_handler",
        task_id=planning_task_id or task_id,
        project_id=project_id,
    )

    if telegram_chat_id:
        try:
            await publish_story_event(
                redis,
                telegram_chat_id=telegram_chat_id,
                event="story_blocked",
                text=(
                    f"Task hit a blocker: {reason[:200]}. "
                    "Our specialist is reviewing — work will continue once resolved."
                ),
                story_id=story_id or "",
                project_id=project_id or "",
            )
        except Exception:
            logger.warning("po_notify_on_gave_up_failed", task_id=task_id, exc_info=True)

    return live_work_settled(
        {
            "status": "gave_up",
            "reason": reason,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )


async def handle_engineering_success(params: EngineeringSuccessParams) -> dict:
    """Handle successful engineering result: CI gate and auto-deploy."""
    result = params.result
    task_id = params.task_id
    project = params.project
    callback_stream = params.callback_stream
    redis = params.redis
    skip_deploy = params.skip_deploy
    telegram_chat_id = params.telegram_chat_id
    action = params.action
    planning_task_id = params.planning_task_id
    story_id = params.story_id
    deploy_fix_attempt = params.deploy_fix_attempt
    project_id = str(project.id)

    if not result.get("commit_sha"):
        logger.error("no_commit_sha", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"runs/{task_id}",
            json={
                "status": RunStatus.FAILED.value,
                "error_message": "Developer completed but no commit was made",
                "result": EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED
                ).model_dump(mode="json"),
                **_observability_patch(params.worker_observability),
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            "Development completed but no code was committed",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )
        return live_work_unsettled(
            {
                "status": "failed",
                "error": "No commit_sha",
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )

    logger.info("engineering_job_success", task_id=task_id, commit_sha=result.get("commit_sha"))

    worker_id = result.get("worker_id")
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
                logger.info("worker_deleted_after_task", worker_id=worker_id)
            except Exception as e:
                logger.warning("worker_delete_failed", worker_id=worker_id, error=str(e))

    run_result = EngineeringRunResult(
        engineering_status=result["engineering_status"],
        commit_sha=result.get("commit_sha"),
        selected_modules=result.get("selected_modules"),
        test_results=result.get("test_results"),
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": run_result.model_dump(mode="json"),
            **_observability_patch(params.worker_observability),
        },
    )

    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, TaskStatus.DONE)
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

    effective_skip_deploy = skip_deploy or bool(planning_task_id)

    logger.info(
        "deploy_decision",
        task_id=task_id,
        planning_task_id=planning_task_id,
        skip_deploy=skip_deploy,
        effective_skip_deploy=effective_skip_deploy,
    )

    if effective_skip_deploy:
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            "Engineering task completed",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )
    else:
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Task completed, deploying...",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )

    deploy_task_id = None
    if not effective_skip_deploy:
        deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
        try:
            await api_client.post(
                "runs/",
                json={
                    "id": deploy_task_id,
                    "type": RunType.DEPLOY.value,
                    "project_id": project_id,
                    "status": RunStatus.QUEUED.value,
                },
            )
            deploy_msg = DeployMessage(
                task_id=deploy_task_id,
                project_id=project_id,
                telegram_chat_id=telegram_chat_id,
                # The deploy inherits the engineering task's recipient. Engineering
                # work that arrived without one stays unaddressed, and says so.
                unaddressed_reason=(
                    "" if telegram_chat_id else "engineering task carried no recipient"
                ),
                callback_stream=callback_stream,
                triggered_by=DeployTrigger.ENGINEERING,
                action=action,
                deploy_fix_attempt=deploy_fix_attempt,
                head_sha=result["commit_sha"],
            )
            await redis.publish_message(DEPLOY_QUEUE, deploy_msg)
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
                telegram_chat_id=telegram_chat_id,
                project_id=project_id,
            )
    else:
        logger.info(
            "deploy_skipped",
            task_id=task_id,
            project_id=project_id,
        )

    return live_work_settled(
        {
            "status": "success",
            "commit_sha": result.get("commit_sha"),
            "deploy_task_id": deploy_task_id,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
