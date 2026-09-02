"""Bounded, truthful operational summary for production administrators."""

from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.admin_overview import (
    AdminOverviewResponse,
    ExecutorDecisionAvailability,
    PaidRunCounts,
    PaidRunStateCounts,
    RecentFailedRun,
    TaskStatusCounts,
    WaitingStory,
)
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryWaitingOn
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import AgentType
from shared.models import Run, Story, Task

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..queue_snapshot import get_queue_snapshot

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_internal_or_admin)]
)

RECENT_FAILED_RUN_LIMIT = 20
WAITING_STORY_LIMIT = 20


def _decision_from_metadata(
    run_type: str, metadata: object
) -> tuple[ExecutorDecision | None, ExecutorDecisionAvailability]:
    """Return only a persisted valid snapshot, never a frontend-side reconstruction."""
    try:
        decision = ExecutorDecision.from_run_metadata(
            metadata if isinstance(metadata, dict) else None
        )
    except ValueError:
        availability = (
            ExecutorDecisionAvailability.LEGACY
            if not isinstance(metadata, dict) or "executor_decision" not in metadata
            else ExecutorDecisionAvailability.INVALID
        )
        return None, availability
    if decision.attempt_kind.value != run_type:
        return None, ExecutorDecisionAvailability.INVALID
    return decision, ExecutorDecisionAvailability.AVAILABLE


def _decision_for_run(run: Run) -> tuple[ExecutorDecision | None, ExecutorDecisionAvailability]:
    return _decision_from_metadata(run.type, run.run_metadata)


def _safe_error_message(run: Run) -> str:
    """Do not expose traceback data through the overview."""
    return (run.error_message or "No safe error message was recorded.")[:2000]


async def build_admin_overview(db: AsyncSession) -> AdminOverviewResponse:
    """Read bounded DB facts plus the one shared queue-health snapshot."""
    queues = await get_queue_snapshot()

    task_rows = await db.execute(select(Task.status, func.count()).group_by(Task.status))
    raw_task_counts = dict(task_rows.all())
    task_counts = TaskStatusCounts(
        **{status.value: raw_task_counts.get(status.value, 0) for status in TaskStatus}
    )

    paid_rows = await db.execute(
        select(Run.status, Run.type, Run.run_metadata).where(
            Run.type.in_([RunType.ENGINEERING.value, RunType.QA.value]),
            Run.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]),
        )
    )
    paid_totals = {RunStatus.QUEUED.value: 0, RunStatus.RUNNING.value: 0}
    by_executor: dict[AgentType, dict[str, int]] = defaultdict(
        lambda: {RunStatus.QUEUED.value: 0, RunStatus.RUNNING.value: 0}
    )
    unavailable_decisions = 0
    for run_status, run_type, metadata in paid_rows.all():
        paid_totals[run_status] += 1
        decision, availability = _decision_from_metadata(run_type, metadata)
        if availability is not ExecutorDecisionAvailability.AVAILABLE or decision is None:
            unavailable_decisions += 1
            continue
        by_executor[decision.agent_type][run_status] += 1

    failed_rows = await db.execute(
        select(Run)
        .where(Run.status == RunStatus.FAILED.value)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(RECENT_FAILED_RUN_LIMIT)
    )
    recent_failed_runs = []
    for run in failed_rows.scalars().all():
        decision, availability = _decision_for_run(run)
        recent_failed_runs.append(
            RecentFailedRun(
                id=run.id,
                type=run.type,
                project_id=run.project_id,
                task_id=run.task_id,
                story_id=run.story_id,
                error_message=_safe_error_message(run),
                created_at=run.created_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                executor_decision=decision,
                executor_decision_availability=availability,
            )
        )

    # Bounded the same way the failed-run list is: most recently touched first,
    # capped, and filtered in SQL so the API never loads every story to find the
    # parked ones.  `waiting_on` is read as written by the transition — the
    # overview derives nothing.
    waiting_rows = await db.execute(
        select(Story.id, Story.project_id, Story.status, Story.waiting_on, Story.updated_at)
        .where(Story.waiting_on != StoryWaitingOn.NONE.value)
        .order_by(Story.updated_at.desc(), Story.id.desc())
        .limit(WAITING_STORY_LIMIT)
    )
    waiting_stories = [
        WaitingStory(
            story_id=story_id,
            project_id=project_id,
            status=story_status,
            waiting_on=waiting_on,
            updated_at=updated_at,
        )
        for story_id, project_id, story_status, waiting_on, updated_at in waiting_rows.all()
    ]

    return AdminOverviewResponse(
        queues=queues,
        task_counts=task_counts,
        paid_runs=PaidRunCounts(
            queued=paid_totals[RunStatus.QUEUED.value],
            running=paid_totals[RunStatus.RUNNING.value],
            by_executor={
                executor: PaidRunStateCounts(queued=counts["queued"], running=counts["running"])
                for executor, counts in by_executor.items()
            },
            unavailable_executor_decisions=unavailable_decisions,
        ),
        recent_failed_runs=recent_failed_runs,
        waiting_stories=waiting_stories,
    )


@router.get("/overview", response_model=AdminOverviewResponse)
async def admin_overview(
    db: AsyncSession = Depends(get_async_session),
) -> AdminOverviewResponse:
    return await build_admin_overview(db)
