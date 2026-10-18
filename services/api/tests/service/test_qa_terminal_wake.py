"""A QA Run's committed first terminal state publishes the QA routing and cleanup hints."""

import uuid

from _api_calls import call_api, committed_run_outcome
from httpx import AsyncClient
import pytest

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget

ROUTING_AND_CLEANUP = [SchedulerWakeTarget.QA_ROUTING, SchedulerWakeTarget.TEMPORARY_ACCESS]
CLEANUP = [SchedulerWakeTarget.TEMPORARY_ACCESS]
PASSED = {"qa_outcome": "passed", "summary": "all green"}
FAILED = {
    "qa_outcome": "failed",
    "failed_checks": [{"name": "GET /health", "detail": "returned 503"}],
}
# A first terminal write, and the late verdict its outcome then refuses.
TERMINAL_WRITES = [
    pytest.param(
        {"status": "completed", "result": PASSED},
        {"status": "completed", "result": FAILED},
        id="passed",
    ),
    pytest.param(
        {"status": "cancelled", "error_message": "temporary access grant was abandoned"},
        {"status": "completed", "result": PASSED},
        id="cancelled_without_result",
    ),
]


async def _running_qa_run(client: AsyncClient, *, story: bool) -> str:
    """A running paid QA Run in its own project, of a story when `story` is set."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await call_api(
        client,
        "POST",
        "/api/users/",
        201,
        json={"telegram_id": telegram_id, "username": f"qa_wake_{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await call_api(
        client,
        "POST",
        "/api/projects/",
        201,
        json={
            "id": project_id,
            "initiating_run_id": f"init-{uuid.uuid4().hex[:8]}",
            "title": "QA terminal wake",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    story_id = None
    if story:
        created = await call_api(
            client,
            "POST",
            "/api/stories/",
            201,
            json={"project_id": project_id, "title": "QA terminal wake"},
        )
        story_id = created["id"]
    admitted = await call_api(
        client,
        "POST",
        "/api/work-admission/paid-runs",
        json={
            "id": f"qa-wake-{uuid.uuid4().hex[:12]}",
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
        },
    )
    assert admitted["admission"]["outcome"] == "admitted"
    run_id = admitted["run_id"]
    await call_api(client, "PATCH", f"/api/runs/{run_id}", json={"status": "running"})
    return run_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal", "late"), TERMINAL_WRITES)
async def test_the_first_terminal_state_publishes_routing_and_cleanup_after_its_commit(
    async_client, wake_hints, db_engine, terminal, late
):
    run_id = await _running_qa_run(async_client, story=True)
    assert await wake_hints.published() == []

    await call_api(async_client, "PATCH", f"/api/runs/{run_id}", json=terminal)

    assert await wake_hints.published() == ROUTING_AND_CLEANUP
    committed = (terminal["status"], terminal.get("result"))
    assert await committed_run_outcome(db_engine, run_id) == committed

    await call_api(async_client, "PATCH", f"/api/runs/{run_id}", json=terminal)
    await call_api(async_client, "PATCH", f"/api/runs/{run_id}", 409, json=late)
    assert await wake_hints.published() == []
    assert await committed_run_outcome(db_engine, run_id) == committed


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal", "late"), TERMINAL_WRITES)
async def test_a_storyless_qa_run_publishes_only_its_cleanup_hint(
    async_client, wake_hints, db_engine, terminal, late
):
    run_id = await _running_qa_run(async_client, story=False)
    assert await wake_hints.published() == []

    await call_api(async_client, "PATCH", f"/api/runs/{run_id}", json=terminal)

    assert await wake_hints.published() == CLEANUP
    assert await committed_run_outcome(db_engine, run_id) == (
        terminal["status"],
        terminal.get("result"),
    )

    await call_api(async_client, "PATCH", f"/api/runs/{run_id}", 409, json=late)
    assert await wake_hints.published() == []
