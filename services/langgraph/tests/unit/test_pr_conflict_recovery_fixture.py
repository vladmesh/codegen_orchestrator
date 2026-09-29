"""Conflict CI readback retains Run columns and immutable Task event history."""

from copy import deepcopy
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from shared.contracts.queues.engineering import EngineeringMessage
from shared.models.run import Run


@pytest.mark.parametrize(
    "change,accepted",
    [
        ("response_order", True),
        ("mutated_details", False),
        ("mutated_status", False),
        ("deleted_history", False),
        ("duplicated_history", False),
        ("duplicated_stop", False),
        ("extra_event", False),
    ],
)
def test_budget_refusal_readback_preserves_complete_history_despite_timestamp_ties(
    change, accepted
):
    path = Path(__file__).resolve().parents[1] / "service/test_pr_conflict_recovery.py"
    spec = importlib.util.spec_from_file_location("conflict_refusal_history_fixture", path)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    # PostgreSQL gives all events in an atomic retry the same transaction timestamp.
    before = [
        {
            "id": event_id,
            "created_at": "2026-09-29T14:24:03.512800Z",
            "event_type": "status_change",
            "from_status": source,
            "to_status": target,
            "details": {"attempt_id": "run-0", "iteration": 0},
        }
        for event_id, source, target in [
            (158, "todo", "in_dev"),
            (159, "in_dev", "failed"),
            (160, "failed", "backlog"),
            (161, "backlog", "todo"),
        ]
    ]
    after = deepcopy(before)
    after.extend(
        {
            "id": event_id,
            "created_at": "2026-09-29T14:24:04.000000Z",
            "event_type": "status_change",
            "from_status": source,
            "to_status": target,
            "details": {"engineering_dispatch_refusal": {"decision_id": "real-decision"}},
        }
        for event_id, source, target in [
            (163, "todo", "in_dev"),
            (164, "in_dev", "waiting_human_review"),
        ]
    )
    if change == "response_order":
        before[:] = [before[1], before[0], *before[2:]]
        after[:] = [*after[1:4], after[0], *after[4:]]
    elif change == "mutated_details":
        after[1]["details"]["iteration"] = 1
    elif change == "mutated_status":
        after[1]["to_status"] = "done"
    elif change == "deleted_history":
        after.pop(1)
    elif change == "duplicated_history":
        after[1] = deepcopy(after[0])
    elif change == "duplicated_stop":
        after[-1] = deepcopy(after[-2])
    elif change == "extra_event":
        after.append(after[-1] | {"id": 165})

    if accepted:
        recovery.assert_refusal_preserves_task_history(before, after)
    else:
        with pytest.raises(AssertionError):
            recovery.assert_refusal_preserves_task_history(before, after)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "iterations,publications,accepted",
    [
        ((0, 1), ("run-0", "run-1"), True),
        ((0, 2), ("run-0", "run-1"), False),
        ((1, 0), ("run-0", "run-1"), False),
        ((0, 1), ("run-0", "run-0"), False),
    ],
    ids=["native-columns", "skipped-iteration", "reversed-history", "duplicate-publication"],
)
async def test_late_start_readback_reaches_bounded_retry_only_for_one_next_attempt(
    monkeypatch, iterations, publications, accepted
):
    path = Path(__file__).resolve().parents[1] / "service/test_pr_conflict_recovery.py"
    spec = importlib.util.spec_from_file_location("conflict_recovery_fixture", path)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)

    # psycopg dict_row returns SQL names, not SQLAlchemy's Python attribute names.
    metadata_column = Run.run_metadata.property.columns[0].name
    runs = [
        {"id": f"run-{i}", metadata_column: {"iteration": iteration}}
        for i, iteration in enumerate(iterations)
    ]
    query = AsyncMock(return_value=runs)
    fail_early = AsyncMock()
    dispatch = Mock()
    finish = AsyncMock()
    monkeypatch.setattr(recovery, "rows", query)
    monkeypatch.setattr(recovery, "fail_before_original_start", fail_early)
    monkeypatch.setattr(recovery, "scheduler", dispatch)
    monkeypatch.setattr(recovery, "finish_repair", finish)
    redis = AsyncMock()
    redis.xrange.return_value = [
        (
            f"{i}-0".encode(),
            {
                b"data": EngineeringMessage(
                    task_id=run_id,
                    project_id="project",
                    initiating_run_id="fixture-init",
                    story_id="story",
                    planning_task_id="pr-conflict-task",
                )
                .model_dump_json()
                .encode()
            },
        )
        for i, run_id in enumerate(publications)
    ]
    api, delayed = object(), object()
    repair = {"id": "pr-conflict-task", "max_iterations": 3}
    args = (api, redis, "story", "project", repair, runs[0], "failed-late-start", delayed)

    if accepted:
        await recovery.finish_admitted_repair(*args)
        finish.assert_awaited_once_with(api, "story", "project", repair, runs[1], "failed")
    else:
        with pytest.raises(AssertionError):
            await recovery.finish_admitted_repair(*args)
        finish.assert_not_awaited()
    fail_early.assert_awaited_once_with(api, redis, "story", repair, runs[0], delayed)
    dispatch.assert_called_once_with("dispatch-start-lost", "story")
