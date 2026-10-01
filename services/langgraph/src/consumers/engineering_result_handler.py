"""Result handlers for engineering worker outcomes (success, gave_up, technical failure)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.engineering_execution import (
    EngineeringExecutionEvidence,
    EngineeringExecutionPhase,
)
from shared.contracts.dto.pr_conflict_repair import PRConflictRepairAttemptDisposition
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import (
    EngineeringFailureReason,
    EngineeringRunResult,
    uncomputable_derived_keys_reason,
)
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode, bounded_diagnostic
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.worker_result import WorkerStopReason
from shared.contracts.vocab import OwnerNotificationEvent
from shared.contracts.worker_turn import AttemptTurnMetadata, WorkerActiveTurn, active_turn_key
from shared.empty_engineering_stop import ensure_empty_story_stop
from shared.notifications import notify_admins_best_effort
from shared.pr_conflict_repair import settle_pr_repair_attempt
from shared.queues import DEPLOY_QUEUE
from shared.redis import RedisStreamClient
from shared.redis.client import decode_redis_fields

from ..clients.api import api_client
from ..clients.story_worker_registry import set_story_worker
from ..clients.worker_spawner import delete_worker, publish_worker_deletion
from ..subgraphs.devops.env_contract_loader import (
    _fetch_env_contract,
    _parse_repo_url,
    uncomputable_required_derived_keys,
)
from ._events import publish_callback_event, publish_story_event
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)


async def prepare_terminal_settlement(
    task_id: str,
    *,
    redis: RedisStreamClient,
    turn_result_consumed: bool,
) -> None:
    """Enforce the turn handoff invariant before a terminal Run write.

    A typed worker result has already settled the recorded turn and leaves a
    story worker available for its next deliberate reuse. Every other terminal
    outcome must first request worker deletion, unless the broker's owner fence
    names a different engineering attempt. Failure to inspect or publish leaves
    the Run non-terminal, so its PEL entry remains reclaimable.
    """
    if turn_result_consumed:
        return

    run = await api_client.get_run(task_id)
    turn = AttemptTurnMetadata.from_run_metadata(run.run_metadata)
    if turn.worker_id is None or turn.active_turn_request_id is None:
        return

    active = WorkerActiveTurn.from_redis_fields(
        decode_redis_fields(await redis.redis.hgetall(active_turn_key(turn.worker_id)))
    )
    if active is not None and active.attempt_id != task_id:
        logger.warning(
            "terminal_settlement_worker_leased_elsewhere",
            task_id=task_id,
            worker_id=turn.worker_id,
            owning_attempt_id=active.attempt_id,
        )
        return

    await publish_worker_deletion(
        redis.redis,
        turn.active_turn_request_id,
        turn.worker_id,
        "failed",
    )
    logger.info(
        "terminal_settlement_worker_teardown_requested",
        task_id=task_id,
        worker_id=turn.worker_id,
        request_id=turn.active_turn_request_id,
        active_turn_request_id=active.request_id if active is not None else None,
    )


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
    turn_result_consumed: bool = True
    execution: EngineeringExecutionEvidence | None = None


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
    elif (factory_evidence := observability.get("factory_evidence")) is not None:
        attempt = {"factory_evidence": factory_evidence}
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


def _attempt_execution_patch(
    stop_reason: WorkerStopReason | None,
    agent_limit_seconds: int | None,
    execution: EngineeringExecutionEvidence | None,
) -> dict:
    """`run_metadata` naming why a turn stopped, or nothing when it did not.

    The API merges `run_metadata`, so an absent stop reason leaves the attempt's
    existing metadata — including the worker and limit recorded at spawn —
    untouched instead of blanking it.
    """
    if stop_reason is None and execution is None:
        return {}
    metadata = {}
    if stop_reason is not None:
        metadata["stop_reason"] = stop_reason.value
    if agent_limit_seconds is not None:
        metadata["agent_limit_seconds"] = agent_limit_seconds
    if execution is not None:
        metadata.update(AttemptTurnMetadata(execution=execution).as_run_metadata())
    return {"run_metadata": metadata}


class StoryStopError(RuntimeError):
    """The empty-result stop was refused; leave the engineering message reclaimable."""


class EmptyResultSettlementError(RuntimeError):
    """A known empty worker outcome must never become a generic terminal failure."""


async def _park_story_without_new_commit(story_id: str, task_id: str, error_msg: str) -> None:
    """Commit the taskless stop with its reason and owed notices before ending the Run."""
    failure = StoryFailure(
        code=StoryFailureCode.NO_NEW_COMMIT,
        source="engineering",
        detail=f"Attempt {task_id}: {error_msg}",
    )
    try:
        await ensure_empty_story_stop(api_client, story_id, failure, actor="engineering-worker")
    except Exception as exc:
        logger.error(
            "story_no_new_commit_stop_failed",
            story_id=story_id,
            task_id=task_id,
            error_type=type(exc).__name__,
        )
        raise StoryStopError(f"story {story_id} stop failed") from None
    logger.warning("engineering_no_new_commit_story_parked", story_id=story_id, task_id=task_id)


async def fail_job(  # noqa: PLR0913 — one attempt's whole context, each part named
    task_id: str,
    error_msg: str,
    planning_task_id: str | None = None,
    worker_observability: dict | None = None,
    stop_reason: WorkerStopReason | None = None,
    agent_limit_seconds: int | None = None,
    *,
    redis: RedisStreamClient,
    execution: EngineeringExecutionEvidence | None = None,
    turn_result_consumed: bool = False,
    story_id: str | None = None,
    failure_reason: EngineeringFailureReason | None = None,
    uncomputable_derived_keys: list[str] | None = None,
    project_id: str = "",
    telegram_chat_id: str = "",
) -> dict:
    """Mark a run as failed and optionally update planning task."""
    try:
        await prepare_terminal_settlement(
            task_id,
            redis=redis,
            turn_result_consumed=turn_result_consumed,
        )
    except Exception as exc:
        if failure_reason is not EngineeringFailureReason.NO_NEW_COMMIT:
            raise
        logger.error(
            "empty_worker_settlement_failed", task_id=task_id, error_type=type(exc).__name__
        )
        raise EmptyResultSettlementError(f"empty result for run {task_id} is not settled") from None
    if failure_reason is EngineeringFailureReason.NO_NEW_COMMIT:
        error_msg = bounded_diagnostic(error_msg)
        # A planned task keeps its existing failed-iteration retry policy. A
        # taskless repair must stop durably before its queue entry can be ACKed.
        if story_id and not planning_task_id:
            await _park_story_without_new_commit(story_id, task_id, error_msg)
    terminal = {
        "status": RunStatus.FAILED.value,
        "error_message": error_msg,
        "result": EngineeringRunResult(
            engineering_status=EngineeringStatus.FAILED,
            failure_reason=failure_reason,
            uncomputable_derived_keys=uncomputable_derived_keys,
            execution=execution,
        ).model_dump(mode="json"),
        **_observability_patch(worker_observability),
        **_attempt_execution_patch(stop_reason, agent_limit_seconds, execution),
    }
    if failure_reason is EngineeringFailureReason.NO_NEW_COMMIT:
        await _write_empty_terminal(task_id, terminal)
    else:
        await api_client.update_run(task_id, terminal)
    if (
        planning_task_id
        and planning_task_id.startswith("pr-conflict-")
        and (
            execution is None
            or execution.execution_phase is not EngineeringExecutionPhase.PRE_AGENT_REFUSED
        )
    ):
        # Early consumer failures may not carry story context; the admitted
        # Run still owns that required identity.
        if story_id is None:
            story_id = (await api_client.get_run(task_id)).story_id
        if story_id is None:
            raise RuntimeError("An admitted conflict repair requires its story")
        await settle_pr_repair_attempt(
            api_client,
            story_id,
            planning_task_id,
            task_id,
            "Engineering attempt failed; settle within the admitted repair bound.",
            PRConflictRepairAttemptDisposition.FAILED,
        )
    elif planning_task_id:
        await _update_task_status(api_client, planning_task_id, TaskStatus.FAILED)
    return live_work_unsettled({"status": "failed", "error": error_msg})


async def _write_empty_terminal(task_id: str, terminal: dict) -> None:
    """Retry one transient write of this exact settled outcome, without another turn.

    The API's immutable terminal writer makes the identical retry safe even
    when a response was lost after commit. All other failures propagate as this
    known outcome's settlement error rather than invoking the generic fallback.
    """
    try:
        try:
            await api_client.update_run(task_id, terminal)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                raise
            logger.warning("empty_run_write_retry", task_id=task_id, error_type=type(exc).__name__)
            await api_client.update_run(task_id, terminal)
    except Exception as exc:
        logger.error("empty_run_write_failed", task_id=task_id, error_type=type(exc).__name__)
        raise EmptyResultSettlementError(f"empty result for run {task_id} is not settled") from None


async def handle_worker_gave_up(
    task_id: str,
    project_id: str,
    planning_task_id: str | None,
    story_id: str | None,
    reason: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    worker_observability: dict | None = None,
    turn_result_consumed: bool = True,
    execution: EngineeringExecutionEvidence | None = None,
) -> dict:
    """Handle worker gave_up: task/story → WHR, admin notified, user informed.

    The worker explicitly could not complete the task and a human needs to
    intervene.
    """
    logger.warning(
        "worker_gave_up",
        task_id=task_id,
        project_id=project_id,
        reason=reason[:200],
    )

    await prepare_terminal_settlement(
        task_id,
        redis=redis,
        turn_result_consumed=turn_result_consumed,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": f"Worker gave up: {reason[:500]}",
            "result": EngineeringRunResult(
                engineering_status=EngineeringStatus.GAVE_UP,
                execution=execution,
            ).model_dump(mode="json"),
            **_observability_patch(worker_observability),
            # A refusal is a stop with a reason, and it is the third one the
            # attempt can carry: the agent declined rather than ran out of time
            # or went quiet.
            **_attempt_execution_patch(WorkerStopReason.AGENT_REFUSED, None, execution),
        },
    )

    settlement = live_work_settled(
        {"status": "gave_up", "reason": reason, "finished_at": datetime.now(UTC).isoformat()}
    )
    if planning_task_id and planning_task_id.startswith("pr-conflict-"):
        if story_id is None:
            raise RuntimeError("An admitted conflict repair requires its story")
        await settle_pr_repair_attempt(
            api_client,
            story_id,
            planning_task_id,
            task_id,
            f"The worker declined repair: {bounded_diagnostic(reason)}",
            PRConflictRepairAttemptDisposition.GAVE_UP,
        )
        return settlement

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
            await api_client.transition_story(story_id, "human-review")
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
                event=OwnerNotificationEvent.STORY_BLOCKED,
                text=(
                    f"Task hit a blocker: {reason[:200]}. "
                    "Work on this story is stopped until a person resolves it; "
                    "there is no known time."
                ),
                story_id=story_id or "",
                project_id=project_id or "",
            )
        except Exception:
            logger.warning("po_notify_on_gave_up_failed", task_id=task_id, exc_info=True)

    return settlement


async def _uncomputable_derived_keys_at(project_id: str, commit_sha: str) -> list[str]:
    """Required derived keys the commit's environment contract declares and no deploy computes.

    The contract is read at the commit, as the deploy will read it. A repository
    or contract that cannot be read or validated finds nothing here: the deploy
    reports it as it always has, and this check adds no failure of its own.
    """
    try:
        repository = await api_client.get_primary_repository(project_id)
        git_url = repository.git_url if repository else None
        parsed = _parse_repo_url(git_url.removesuffix(".git")) if git_url else None
        if parsed is None:
            return []
        contract = await _fetch_env_contract(*parsed, commit_sha)
        return uncomputable_required_derived_keys(contract) if contract else []
    except Exception as error:
        logger.warning(
            "derived_key_check_skipped",
            project_id=project_id,
            commit_sha=commit_sha,
            error_type=type(error).__name__,
        )
        return []


async def publish_empty_result_callback(
    redis: RedisStreamClient,
    callback_stream: str | None,
    task_id: str,
    message: str,
    *,
    telegram_chat_id: str,
    project_id: str,
) -> None:
    """An optional callback cannot undo the committed empty-result disposition."""
    try:
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            bounded_diagnostic(message),
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )
    except Exception as exc:
        logger.warning(
            "empty_result_callback_failed", task_id=task_id, error_type=type(exc).__name__
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
        outcome = await fail_job(
            task_id,
            "Developer completed but no commit was made",
            planning_task_id,
            params.worker_observability,
            redis=redis,
            execution=params.execution,
            turn_result_consumed=params.turn_result_consumed,
            story_id=story_id,
            failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
            project_id=project_id,
            telegram_chat_id=telegram_chat_id,
        )
        await publish_empty_result_callback(
            redis,
            callback_stream,
            task_id,
            "Development completed but no code was committed",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )
        return outcome

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

    # A required derived key no deploy can compute fails every deploy of this
    # commit in the secret resolver. It is the developer's to fix, so the attempt
    # fails here, before a task is done or a deploy is triggered.
    uncomputable = await _uncomputable_derived_keys_at(project_id, result["commit_sha"])
    if uncomputable:
        reason = uncomputable_derived_keys_reason(uncomputable)
        logger.warning(
            "engineering_commit_declares_uncomputable_derived_keys",
            task_id=task_id,
            project_id=project_id,
            commit_sha=result["commit_sha"],
            keys=uncomputable,
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            reason,
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )
        return await fail_job(
            task_id,
            reason,
            planning_task_id,
            params.worker_observability,
            redis=redis,
            execution=params.execution,
            turn_result_consumed=params.turn_result_consumed,
            story_id=story_id,
            failure_reason=EngineeringFailureReason.UNCOMPUTABLE_DERIVED_KEY,
            uncomputable_derived_keys=uncomputable,
            project_id=project_id,
            telegram_chat_id=telegram_chat_id,
        )

    run_result = EngineeringRunResult(
        engineering_status=result["engineering_status"],
        commit_sha=result.get("commit_sha"),
        execution=params.execution,
    )
    await prepare_terminal_settlement(
        task_id,
        redis=redis,
        turn_result_consumed=params.turn_result_consumed,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": run_result.model_dump(mode="json"),
            **_observability_patch(params.worker_observability),
            **_attempt_execution_patch(None, None, params.execution),
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
                    # The Run says which story it belongs to. Without it this
                    # deploy is invisible to every story-scoped reader, and a
                    # follow-up wait watching the story sees no deploy at all
                    # however quickly this one settles.
                    "story_id": story_id,
                    "status": RunStatus.QUEUED.value,
                    "run_metadata": {"head_sha": result["commit_sha"]},
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
                # Deliberately no `deployed_commit_sha`: this path deploys the
                # worker's own commit, which lives on a story branch, and the
                # generated project's CI publishes images only from its default
                # branch. There is no built commit to name, so the deploy is
                # refused by the resolver instead of pulling whatever the
                # registry happens to hold — which is what it used to do.
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
