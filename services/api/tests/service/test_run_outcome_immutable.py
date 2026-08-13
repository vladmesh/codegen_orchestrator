"""A run that reached a terminal state keeps that answer.

Refusing only the move back to a live status leaves the interleaving that
matters open: a supervisor ends a run the worker is still inside, and the
worker's own terminal write lands afterwards. Both writes are terminal, so
nothing stops the later one from replacing a named failure with a pass — and the
story supervisor reads whatever is there.

Cancellation is an outcome even when it has no typed result. A worker that
finishes after it was cancelled has a stale verdict, not permission to replace
the cancellation.
"""

import asyncio
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models import Run


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
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "Run Outcome",
            "config": {},
        },
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
async def test_a_cancelled_run_refuses_a_late_worker_verdict(async_client: AsyncClient):
    """The central QA PATCH cannot replace a cancellation it outlived."""
    run_id = await _run(async_client)

    cancelled = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "cancelled", "error_message": "temporary access grant was abandoned"},
    )
    assert cancelled.status_code == status.HTTP_200_OK
    first_completed_at = cancelled.json()["completed_at"]

    late_verdict = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {"qa_outcome": "passed", "summary": "all good"},
        },
    )

    assert late_verdict.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "cancelled"
    assert run.json()["result"] is None
    assert run.json()["error_message"] == "temporary access grant was abandoned"
    assert run.json()["completed_at"] == first_completed_at


@pytest.mark.asyncio
async def test_repeating_an_outcome_after_a_lost_response_is_not_a_conflict(
    async_client: AsyncClient,
):
    """A writer re-sending its own answer is not racing anybody."""
    run_id = await _run(async_client)
    payload = {"status": "completed", "result": {"qa_outcome": "passed", "report": "all good"}}

    first = await async_client.patch(f"/api/runs/{run_id}", json=payload)
    first_completed_at = first.json()["completed_at"]
    second = await async_client.patch(
        f"/api/runs/{run_id}",
        json={**payload, "completed_at": "2030-01-01T00:00:00Z"},
    )

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["result"]["qa_outcome"] == "passed"
    assert first_completed_at is not None
    assert second.json()["completed_at"] == first_completed_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_outcome"),
    [
        (
            {"status": "completed", "result": {"qa_outcome": "passed"}},
            "completed",
            "passed",
        ),
        (
            {
                "status": "completed",
                "result": {
                    "qa_outcome": "failed",
                    "failed_checks": [{"name": "GET /health", "detail": "returned 503"}],
                },
            },
            "completed",
            "failed",
        ),
        ({"status": "cancelled"}, "cancelled", None),
    ],
    ids=("success", "failed_verdict", "cancellation"),
)
async def test_first_qa_terminal_transition_stamps_completion_time(
    async_client: AsyncClient,
    payload: dict,
    expected_status: str,
    expected_outcome: str | None,
):
    """The API commits a QA verdict and its completion timestamp together."""
    run_id = await _run(async_client)

    settled = await async_client.patch(f"/api/runs/{run_id}", json=payload)

    assert settled.status_code == status.HTTP_200_OK
    assert settled.json()["status"] == expected_status
    assert settled.json()["completed_at"] is not None
    assert (settled.json()["result"] or {}).get("qa_outcome") == expected_outcome


@pytest.mark.asyncio
async def test_a_preterminal_client_completion_time_is_ignored(async_client: AsyncClient):
    """Only the terminal transition may write the completion timestamp."""
    run_id = await _run(async_client)
    supplied_at = "2030-01-01T00:00:00Z"

    updated = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"run_metadata": {"stage": "QA in progress"}, "completed_at": supplied_at},
    )

    assert updated.status_code == status.HTTP_200_OK
    assert updated.json()["completed_at"] is None

    settled = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed"}},
    )

    assert settled.status_code == status.HTTP_200_OK
    assert settled.json()["completed_at"] is not None
    assert settled.json()["completed_at"] != supplied_at


@pytest.mark.asyncio
async def test_a_pass_decided_before_the_failure_landed_cannot_overwrite_it(
    async_client: AsyncClient,
    db_engine,
):
    """The refusal has to survive the two writers overlapping, not just following.

    Checking the stored outcome and then writing over it are two steps, and the
    sweep's named access failure commits between them: the QA worker reads a run
    that is still running, passes every rule, and its pass lands on top. Both
    writers therefore have to take the run's row before they read it, which is
    what this drives — the failure is written by a transaction that is still open
    while the worker's pass is already in the endpoint.
    """
    run_id = await _run(async_client)
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with sessions() as sweep:
        run = (
            await sweep.execute(select(Run).where(Run.id == run_id).with_for_update())
        ).scalar_one()
        run.status = "failed"
        run.error_message = "temporary access QA_TEST_TELEGRAM_ID expired while QA was running"
        run.result = _blocked_result()

        worker = asyncio.create_task(
            async_client.patch(
                f"/api/runs/{run_id}",
                json={
                    "status": "completed",
                    "result": {"qa_outcome": "passed", "report": "all good"},
                },
            )
        )
        # Long enough for the request to reach the endpoint and stop there.
        await asyncio.sleep(1)
        assert not worker.done(), "the pass decided its answer without waiting for the row"
        await sweep.commit()

    passed = await worker
    assert passed.status_code == status.HTTP_409_CONFLICT

    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "failed"
    assert run.json()["result"]["blocker"]["category"] == "qa_access_expired"


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
