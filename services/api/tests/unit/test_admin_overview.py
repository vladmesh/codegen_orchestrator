"""Unit tests for the bounded administrator operational overview."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.admin_overview import (
    AdminOverviewResponse,
    PaidRunCounts,
    QueueBindingSnapshot,
    QueueHealthSnapshot,
    TaskStatusCounts,
)
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus, StoryWaitingOn
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import AgentType
from src.main import app
from src.routers.admin_overview import _decision_for_run, _safe_error_message, build_admin_overview


@pytest.mark.asyncio
async def test_admin_overview_is_internal_only_and_returns_typed_empty_state():
    """The overview remains an operational surface and preserves an empty DB state."""
    overview = AdminOverviewResponse(
        queues=QueueHealthSnapshot(
            status="ok",
            bindings=[
                QueueBindingSnapshot(
                    stream="engineering:queue",
                    group="capability-workers",
                    description="engineering",
                    stream_info={"length": 0},
                    group_info={"consumers": 0, "pending": 0, "last_delivered_id": "0-0"},
                )
            ],
            issues=[],
        ),
        task_counts=TaskStatusCounts(**{status.value: 0 for status in TaskStatus}),
        paid_runs=PaidRunCounts(
            queued=0,
            running=0,
            by_executor={},
            unavailable_executor_decisions=0,
        ),
        recent_failed_runs=[],
        waiting_stories=[],
    )

    with patch(
        "src.routers.admin_overview.build_admin_overview", new=AsyncMock(return_value=overview)
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            forbidden = await client.get("/api/admin/overview")
            response = await client.get("/api/admin/overview", headers=INTERNAL_HEADERS)

    assert forbidden.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["task_counts"]["waiting_human_review"] == 0
    assert body["task_counts"]["failed"] == 0
    assert body["paid_runs"][RunStatus.QUEUED.value] == 0
    assert body["recent_failed_runs"] == []
    assert body["waiting_stories"] == []


@pytest.mark.parametrize("agent", [AgentType.CLAUDE, AgentType.CODEX])
def test_overview_uses_only_valid_persisted_executor_decisions(agent: AgentType):
    decision = ExecutorDecision(
        attempt_kind="engineering",
        agent_type=agent,
        source="api_default",
        policy_version="v2",
        reason="configured default",
    )
    run = SimpleNamespace(type="engineering", run_metadata=decision.as_run_metadata())

    parsed, availability = _decision_for_run(run)

    assert parsed == decision
    assert availability == "available"


@pytest.mark.parametrize(
    ("metadata", "availability"),
    [
        ({}, "legacy"),
        ({"executor_decision": {"agent_type": "codex"}}, "invalid"),
        (
            {
                "executor_decision": {
                    "attempt_kind": "engineering",
                    "agent_type": "codex",
                    "source": "api_default",
                    "policy_version": "v2",
                    "reason": "configured default",
                    "unexpected": "must not be discarded",
                }
            },
            "invalid",
        ),
    ],
)
def test_overview_marks_legacy_and_malformed_decisions_unavailable(metadata, availability):
    run = SimpleNamespace(type="engineering", run_metadata=metadata)

    parsed, actual = _decision_for_run(run)

    assert parsed is None
    assert actual == availability


def test_overview_never_uses_traceback_and_bounds_safe_error_text():
    run = SimpleNamespace(error_message="x" * 3000, error_traceback="secret traceback")

    assert _safe_error_message(run) == "x" * 2000


@pytest.mark.asyncio
async def test_overview_counts_all_task_statuses_and_only_persisted_paid_decisions():
    decision = ExecutorDecision(
        attempt_kind="engineering",
        agent_type="codex",
        source="api_default",
        policy_version="v2",
        reason="configured default",
    )
    failed = SimpleNamespace(
        id="failed-1",
        type="engineering",
        project_id=None,
        task_id=None,
        story_id=None,
        error_message="safe error",
        error_traceback="never returned",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        started_at=None,
        completed_at=None,
        run_metadata=decision.as_run_metadata(),
    )
    results = []
    task_result = MagicMock()
    task_result.all.return_value = [(status.value, 1) for status in TaskStatus]
    results.append(task_result)
    paid_result = MagicMock()
    paid_result.all.return_value = [
        ("queued", "engineering", decision.as_run_metadata()),
        ("running", "qa", {}),
        (
            "running",
            "engineering",
            {
                "executor_decision": {
                    "attempt_kind": "engineering",
                    "agent_type": "codex",
                    "source": "api_default",
                    "policy_version": "v2",
                    "reason": "configured default",
                    "unexpected": "must not be discarded",
                }
            },
        ),
    ]
    results.append(paid_result)
    failed_result = MagicMock()
    failed_result.scalars.return_value.all.return_value = [failed]
    results.append(failed_result)
    waiting_result = MagicMock()
    waiting_result.all.return_value = [
        (
            "story-1",
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            StoryStatus.DEPLOYING.value,
            StoryWaitingOn.DEPLOY.value,
            datetime(2026, 1, 3, tzinfo=UTC),
        )
    ]
    results.append(waiting_result)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    queues = QueueHealthSnapshot(status="ok", bindings=[], issues=[])

    with patch("src.routers.admin_overview.get_queue_snapshot", new=AsyncMock(return_value=queues)):
        overview = await build_admin_overview(db)

    assert all(count == 1 for count in overview.task_counts.model_dump().values())
    assert overview.paid_runs.queued == 1
    assert overview.paid_runs.running == 2
    assert overview.paid_runs.by_executor[AgentType.CODEX].queued == 1
    assert overview.paid_runs.unavailable_executor_decisions == 2
    assert overview.recent_failed_runs[0].executor_decision == decision
    assert overview.recent_failed_runs[0].error_message == "safe error"
    # The overview reports the wait the transition wrote; it derives none of it.
    assert [
        (story.story_id, story.status, story.waiting_on) for story in overview.waiting_stories
    ] == [("story-1", StoryStatus.DEPLOYING, StoryWaitingOn.DEPLOY)]
