"""A run that recorded how it ended keeps that answer.

Refusing only the move back to a live status leaves the interleaving that
matters open: a supervisor ends a run the worker is still inside, and the
worker's own terminal write lands afterwards. Both writes are terminal, so
nothing stops the later one from replacing a named failure with a pass — and the
story supervisor reads whatever is there.

The rule cannot be "terminal runs are frozen" either. A cancelled deploy is
marked terminal by whoever cancelled it, and the worker that owned it records
what it actually did afterwards; that record is the only proof its dispatch is
over.
"""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest


async def _run(async_client: AsyncClient, *, run_type: str = "qa") -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"outcome_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    project = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "Run Outcome", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED

    run_id = f"{run_type}-{uuid.uuid4().hex[:8]}"
    run = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": run_type, "project_id": project_id},
    )
    assert run.status_code == status.HTTP_201_CREATED
    return run_id


def _blocked_result() -> dict:
    return {
        "qa_outcome": "blocked",
        "summary": "temporary test access outlived the QA run it was granted for",
        "blocker": {
            "category": "qa_access_expired",
            "attempted": "keep temporary access QA_TEST_TELEGRAM_ID",
            "sent": "grant tempaccess-qa-1 issued 40 minutes ago",
            "received": "QA run still running after 30 minutes",
        },
    }


@pytest.mark.asyncio
async def test_a_settled_run_refuses_a_second_terminal_outcome(async_client: AsyncClient):
    """The exact race: expired access fails the run, QA then reports it passed.

    Without this the story supervisor reads `passed` on a run that ended because
    the identity it was testing with was taken away, and completes the story.
    """
    run_id = await _run(async_client)

    expired = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "failed",
            "error_message": "temporary access QA_TEST_TELEGRAM_ID expired while QA was running",
            "result": _blocked_result(),
        },
    )
    assert expired.status_code == status.HTTP_200_OK

    passed = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed", "report": "all good"}},
    )

    assert passed.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "failed"
    assert run.json()["result"]["qa_outcome"] == "blocked"
    assert run.json()["result"]["blocker"]["category"] == "qa_access_expired"


@pytest.mark.asyncio
async def test_the_first_terminal_outcome_wins_whichever_it_is(async_client: AsyncClient):
    """The other order of the same race: QA finishes before the sweep gives up."""
    run_id = await _run(async_client)

    passed = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed", "report": "all good"}},
    )
    assert passed.status_code == status.HTTP_200_OK

    expired = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "failed", "result": _blocked_result()},
    )

    assert expired.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["result"]["qa_outcome"] == "passed"


@pytest.mark.asyncio
async def test_a_cancelled_run_may_still_record_what_its_worker_did(async_client: AsyncClient):
    """Cancelling names no outcome, so the worker's result is the first one."""
    run_id = await _run(async_client, run_type="deploy")

    withdrawn = await async_client.post(
        f"/api/runs/{run_id}/dispatch-withdraw",
        params={"reason": "temporary access grant was abandoned"},
    )
    assert withdrawn.json()["run_status"] == "cancelled"

    recorded = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "cancelled",
            "error_message": "Deploy was cancelled before it could finish",
            "result": {"deploy_outcome": "cancelled"},
        },
    )

    assert recorded.status_code == status.HTTP_200_OK
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["result"]["deploy_outcome"] == "cancelled"
    assert run.json()["error_message"] == "Deploy was cancelled before it could finish"


@pytest.mark.asyncio
async def test_repeating_an_outcome_after_a_lost_response_is_not_a_conflict(
    async_client: AsyncClient,
):
    """A writer re-sending its own answer is not racing anybody."""
    run_id = await _run(async_client)
    payload = {"status": "completed", "result": {"qa_outcome": "passed", "report": "all good"}}

    first = await async_client.patch(f"/api/runs/{run_id}", json=payload)
    second = await async_client.patch(f"/api/runs/{run_id}", json=payload)

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["result"]["qa_outcome"] == "passed"


@pytest.mark.asyncio
async def test_a_settled_run_still_accepts_notes_that_are_not_its_outcome(
    async_client: AsyncClient,
):
    """Metadata and accounting are not the answer; only status/result/error are."""
    run_id = await _run(async_client)
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed"}},
    )

    noted = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"run_metadata": {"transcript": "s3://bucket/key"}, "total_tokens": 4212},
    )

    assert noted.status_code == status.HTTP_200_OK
    assert noted.json()["run_metadata"]["transcript"] == "s3://bucket/key"
    assert noted.json()["result"]["qa_outcome"] == "passed"
