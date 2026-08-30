"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.consumers.engineering
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus

import httpx
from pydantic import ValidationError
import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import AllocationFailureReason, EngineeringRunResult
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import ActionType
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.queues import ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import get_story_worker
from ..nodes.resource_allocator import resource_allocator_node
from ._base import start_worker
from ._events import publish_callback_event
from ._live_work import live_work_unsettled
from ._repo_setup import _create_repo_and_set_secrets
from ._validation import _safe_validation_errors
from .engineering_result_handler import (
    EngineeringSuccessParams,
    _update_task_status,
    _write_task_event,
    fail_job as _fail_job,
    handle_engineering_success as _handle_engineering_success,
    handle_worker_gave_up as _handle_worker_gave_up,
    prepare_terminal_settlement,
)
from .story_context import (
    build_story_context as _build_story_context,
    build_story_md as _build_story_md,
)

# How many engineering jobs one consumer runs at once. Operator-owned at
# runtime: raising it lets a second user's project start while the first is
# still working, lowering it drains back to sequential without a redeploy.
ENGINEERING_SLOTS_CONFIG_KEY = "engineering.worker_slots"

# Re-export for backward compatibility with tests
__all__ = [
    "_build_story_context",
    "_build_story_md",
    "_fail_job",
    "_handle_engineering_success",
    "_handle_worker_gave_up",
    "_update_task_status",
    "_write_task_event",
    "process_engineering_job",
]


def _parse_telegram_id(telegram_chat_id: str) -> dict:
    """Build get_project kwargs with telegram_id if telegram_chat_id is numeric."""
    if telegram_chat_id and telegram_chat_id.isdigit():
        return {"telegram_id": int(telegram_chat_id)}
    return {}


async def _handle_invalid_engineering_message(
    job_data: dict, exc: ValidationError, redis: RedisStreamClient
) -> dict:
    """Terminal handling for a malformed job: log only safe error fields, fail the run so the
    outcome is durable, and return so the queue loop ACKs the poison entry.

    The entry is ACKed only once a terminal outcome is durably written. If failing the run
    hits a transient API error (5xx or a transport error) the outcome is lost, so re-raise —
    the loop then leaves the entry unacked and `claim_pending` retries after the API recovers.
    Only a non-retryable client error (e.g. 404 — no such run to fail) is ACKed, to avoid an
    eternal poison-loop on a run that will never accept the write.
    """
    raw_task_id = job_data.get("task_id")
    logger.error(
        "engineering_job_invalid_message",
        task_id=raw_task_id,
        errors=_safe_validation_errors(exc),
    )
    if not (isinstance(raw_task_id, str) and raw_task_id):
        # No identifiable run — nothing durable to lose; ACK so it does not reclaim forever.
        return live_work_unsettled({"status": "failed", "error": "invalid_engineering_message"})

    try:
        return await _fail_job(raw_task_id, "invalid engineering message", None, redis=redis)
    except httpx.HTTPStatusError as fail_exc:
        if fail_exc.response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.warning(
                "engineering_invalid_message_run_unwritable",
                task_id=raw_task_id,
                status_code=fail_exc.response.status_code,
            )
            return live_work_unsettled({"status": "failed", "error": "invalid_engineering_message"})
        # Transient server error — terminal outcome not written; do not ACK.
        raise


logger = structlog.get_logger(__name__)


async def _resolve_allocations(
    task_id: str, project_id: str, project: ProjectDTO, redis: RedisStreamClient
) -> dict | None:
    """Resolve or create resource allocations. Returns dict or None on failure."""
    logger.info("allocating_resources", task_id=task_id, project_id=project_id)
    result = await resource_allocator_node.run(
        {
            "project_id": project_id,
            "project_spec": project.model_dump(),
            "allocated_resources": {},
            "errors": [],
        }
    )
    if result.get("errors"):
        error_msg = "; ".join(result["errors"])
        logger.error("resource_allocation_failed", task_id=task_id, errors=result["errors"])
        reason = result.get("allocation_failure_reason")
        required_ram_mb = result.get("allocation_required_ram_mb")
        min_disk_mb = result.get("allocation_min_disk_mb")
        if reason and (not isinstance(required_ram_mb, int) or not isinstance(min_disk_mb, int)):
            raise RuntimeError("allocation failure omitted admission requirements")
        await prepare_terminal_settlement(task_id, redis=redis, turn_result_consumed=False)
        await api_client.patch(
            f"runs/{task_id}",
            json={
                "status": RunStatus.FAILED.value,
                "error_message": error_msg,
                "result": EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason(reason) if reason else None,
                    allocation_required_ram_mb=required_ram_mb,
                    allocation_min_disk_mb=min_disk_mb,
                ).model_dump(mode="json"),
            },
        )
        return None

    allocated = result.get("allocated_resources", {})
    logger.info("resources_allocated", task_id=task_id, count=len(allocated))
    return allocated


async def _load_engineering_executor_decision(task_id: str) -> ExecutorDecision:
    """Read the persisted paid-start policy before the developer graph can launch."""
    run = await api_client.get_run(task_id)
    decision = ExecutorDecision.from_run_metadata(run.run_metadata)
    if decision.attempt_kind is not RunType.ENGINEERING:
        raise ValueError("expected engineering attempt")
    return decision


async def _recorded_attempt_turn(task_id: str) -> AttemptTurnMetadata:
    """Read the durable handoff record for this still-running engineering attempt."""
    run = await api_client.get_run(task_id)
    return AttemptTurnMetadata.from_run_metadata(run.run_metadata)


async def _existing_attempt_worker(
    redis: RedisStreamClient,
    *,
    story_id: str | None,
    task_id: str,
    attempt_turn: AttemptTurnMetadata,
) -> str | None:
    """Resolve an existing worker from durable ownership records only."""
    if story_id:
        worker_id = await get_story_worker(redis.redis, story_id)
        if worker_id:
            logger.info(
                "reusing_story_worker",
                story_id=story_id,
                worker_id=worker_id,
                task_id=task_id,
            )
            return worker_id
    if attempt_turn.worker_id:
        logger.info(
            "adopting_recorded_engineering_turn",
            worker_id=attempt_turn.worker_id,
            task_id=task_id,
        )
    return attempt_turn.worker_id


async def process_engineering_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single engineering job by running Engineering Subgraph."""
    from ..subgraphs.engineering import create_engineering_subgraph

    # Typed boundary: validate before business logic. A malformed job is terminal (ACKed),
    # not a poison message that reclaims forever.
    try:
        msg = EngineeringMessage.model_validate(job_data)
    except ValidationError as exc:
        return await _handle_invalid_engineering_message(job_data, exc, redis)

    task_id = msg.task_id
    project_id = msg.project_id
    callback_stream = msg.callback_stream
    action = msg.action
    description = msg.description
    skip_deploy = msg.skip_deploy
    telegram_chat_id = msg.telegram_chat_id
    planning_task_id = msg.planning_task_id
    story_id = msg.story_id
    deploy_fix_attempt = msg.deploy_fix_attempt

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        await api_client.patch(
            f"runs/{task_id}",
            json={"status": RunStatus.RUNNING.value, "started_at": datetime.now(UTC).isoformat()},
        )

        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Engineering task started",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id or "",
        )

        project = await api_client.get_project(project_id, **_parse_telegram_id(telegram_chat_id))
        if not project:
            return await _fail_job(
                task_id, f"Project {project_id} not found", planning_task_id, redis=redis
            )

        try:
            executor_decision = await _load_engineering_executor_decision(task_id)
        except ValueError as exc:
            return await _fail_job(
                task_id,
                f"Invalid executor decision snapshot: {exc}",
                planning_task_id,
                redis=redis,
            )

        if not description:
            description = (project.config or {}).get("description", "")

        project_status = project.status
        if project_status == ProjectStatus.DRAFT and action == ActionType.CREATE:
            await _create_repo_and_set_secrets(project)
        elif project_status == ProjectStatus.DRAFT and action != ActionType.CREATE:
            logger.warning(
                "feature_fix_on_draft_project",
                task_id=task_id,
                project_id=project_id,
                action=action,
                hint="Project is in draft status but action is not 'create'. "
                "Skipping scaffolding — developer will work with existing repo.",
            )

        allocated_resources = await _resolve_allocations(task_id, project_id, project, redis)
        if allocated_resources is None:
            return live_work_unsettled({"status": "failed", "error": "Resource allocation failed"})

        attempt_turn = await _recorded_attempt_turn(task_id)
        existing_worker_id = await _existing_attempt_worker(
            redis,
            story_id=story_id,
            task_id=task_id,
            attempt_turn=attempt_turn,
        )

        primary_repo = await api_client.get_primary_repository(project_id)
        repo_id = primary_repo.id if primary_repo else None

        story_context = await _build_story_context(story_id, planning_task_id) if story_id else None
        story_md = await _build_story_md(story_id, planning_task_id) if story_id else None

        branch = f"story/{story_id}" if story_id else None

        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project.model_dump(),
            "allocated_resources": allocated_resources,
            "action": action,
            "description": description,
            "story_context": story_context,
            "story_md": story_md,
            "repo_id": repo_id,
            # `msg.task_id` is the engineering run's id (task_dispatcher creates
            # the run and names the message after it). It travels into the
            # subgraph as what it is: one attempt inside the initiating run.
            "run_id": task_id,
            # Who every worker this subgraph asks for belongs to, derived once,
            # here, from the message that started the work — the project, the
            # run that initiated it, and this attempt. The nodes below stamp
            # this value; none of them recomputes it, so nothing downstream can
            # substitute a different identity.
            "ownership": WorkerOwnership.for_engineering(msg),
            "executor_decision": executor_decision,
            "commit_sha": None,
            "worker_id": existing_worker_id,
            "attempt_turn": attempt_turn,
            "engineering_status": EngineeringStatus.IDLE,
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "branch": branch,
            "story_id": story_id,
            "worker_report": None,
            "worker_observability": None,
            "gave_up_reason": None,
            "stop_reason": None,
            "agent_limit_seconds": None,
            "turn_result_consumed": False,
            "errors": [],
        }

        engineering_subgraph = create_engineering_subgraph()
        developer_started_at = datetime.now(UTC)
        result = await engineering_subgraph.ainvoke(subgraph_input)

        worker_report = result.get("worker_report")
        if worker_report and planning_task_id:
            await _write_task_event(
                api_client,
                planning_task_id,
                "worker_report",
                {"report": worker_report},
            )
            logger.info(
                "worker_report_saved",
                task_id=task_id,
                planning_task_id=planning_task_id,
                report_size=len(worker_report),
            )

        eng_status = result.get("engineering_status", EngineeringStatus.FAILED)

        if eng_status == EngineeringStatus.DONE:
            logger.info(
                "engineering_job_success",
                task_id=task_id,
                commit_sha=result.get("commit_sha"),
            )
            return await _handle_engineering_success(
                EngineeringSuccessParams(
                    result=result,
                    task_id=task_id,
                    project=project,
                    callback_stream=callback_stream,
                    redis=redis,
                    skip_deploy=skip_deploy,
                    developer_started_at=developer_started_at,
                    telegram_chat_id=telegram_chat_id,
                    action=action,
                    planning_task_id=planning_task_id,
                    story_id=story_id,
                    deploy_fix_attempt=deploy_fix_attempt,
                    worker_observability=result.get("worker_observability"),
                    turn_result_consumed=result.get("turn_result_consumed", True),
                )
            )

        elif eng_status == EngineeringStatus.GAVE_UP:
            reason = result.get("gave_up_reason") or "Worker could not complete the task"
            return await _handle_worker_gave_up(
                task_id=task_id,
                project_id=project_id,
                planning_task_id=planning_task_id,
                story_id=story_id,
                reason=reason,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                worker_observability=result.get("worker_observability"),
                turn_result_consumed=result.get("turn_result_consumed", True),
            )
        else:
            # FAILED (technical) or unexpected status — treat as technical failure
            errors = result.get("errors", ["Unknown engineering status"])
            error_msg = "; ".join(errors)
            logger.error("engineering_job_failed_status", task_id=task_id, errors=errors)
            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                error_msg,
                telegram_chat_id=telegram_chat_id,
                project_id=project_id or "",
            )
            return await _fail_job(
                task_id,
                error_msg,
                planning_task_id,
                result.get("worker_observability"),
                stop_reason=result.get("stop_reason"),
                agent_limit_seconds=result.get("agent_limit_seconds"),
                redis=redis,
                turn_result_consumed=result.get("turn_result_consumed", False),
            )

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            f"Engineering task failed: {e!s}",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id or "",
        )
        return await _fail_job(task_id, str(e), planning_task_id, redis=redis)


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="engineering-worker",
        queue=ENGINEERING_QUEUE,
        process_fn=process_engineering_job,
        slots_config_key=ENGINEERING_SLOTS_CONFIG_KEY,
    )


if __name__ == "__main__":
    main()
