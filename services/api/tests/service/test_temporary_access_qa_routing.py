"""Cleanup escalation waits until the story consumed this QA run's verdict.

The routing stamp is written on the QA run by the story transition that routes
it, in that transition's transaction, and the escalation reads it under the
same run row lock. So escalation and routing can race in any order and the
cleanup incident never lands on a verdict its story has not consumed.
"""

import asyncio
import uuid

from fastapi import status
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QA_ROUTED_KEY, QAHandoffPlan
from shared.contracts.dto.temporary_access import QA_ROUTING_PENDING
from shared.contracts.queues.qa import QAMessage
from shared.models import Run, TemporaryAccessGrant

HEAD_SHA = "b" * 40
PASSED = {"qa_outcome": "passed", "summary": "the bot answers /start"}
ESCALATION = {
    "error": "revoke proof failed",
    "run_error_message": "temporary QA access could not be revoked",
    "run_result": {
        "qa_outcome": "blocked",
        "summary": "temporary QA access could not be revoked",
        "blocker": {
            "category": "qa_cleanup_failed",
            "attempted": "revoke temporary QA access",
            "sent": "capability revoke",
            "received": "inactive readback was not proved",
        },
    },
}


async def _testing_story(async_client) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"route_{telegram_id}"}
    )
    assert user.status_code == status.HTTP_201_CREATED
    project_id = str(uuid.uuid4())
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "initiating_run_id": f"init-{uuid.uuid4().hex[:8]}",
            "title": "QA routing guard",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED
    story = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Routing"}
    )
    assert story.status_code == status.HTTP_201_CREATED, story.text
    story_id = story.json()["id"]
    for action in ("start", "deploy", "test"):
        moved = await async_client.post(f"/api/stories/{story_id}/{action}")
        assert moved.status_code == status.HTTP_200_OK, moved.text
    return project_id, story_id


async def _qa_run(async_client, project_id: str, story_id: str) -> str:
    run_id = f"qa-{uuid.uuid4().hex[:8]}"
    # A real QA run is created with its handoff plan; completing a story on a
    # passed verdict reads the deployed address from it.
    handoff = QAHandoffPlan(
        qa_message=QAMessage(
            story_id=story_id,
            project_id=project_id,
            initiating_run_id="live-1",
            deployed_url="https://exact.example.com",
            application_id=42,
            acceptance_criteria="the bot answers /start",
            run_id=run_id,
        )
    ).model_dump(mode="json")
    run = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": run_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {QA_HANDOFF_KEY: handoff},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.text
    return run.json()["run_id"]


async def _settle(async_client, run_id: str, result: dict = PASSED) -> None:
    settled = await async_client.patch(
        f"/api/runs/{run_id}", json={"status": "completed", "result": result}
    )
    assert settled.status_code == status.HTTP_200_OK, settled.text


async def _grant(async_client, project_id: str, run_id: str) -> str:
    grant_id = f"tempaccess-{run_id}"
    created = await async_client.post(
        "/api/temporary-access-grants/",
        json={
            "id": grant_id,
            "project_id": project_id,
            "channel": "telegram",
            "external_id": "8202532144",
            "target_application_id": uuid.uuid4().int % 1_000_000 + 1,
            "target_base_url": "https://exact.example.com",
            "head_sha": HEAD_SHA,
            "qa_run_id": run_id,
            "grant_run_id": f"temporary-access-grant-{uuid.uuid4().hex[:8]}",
            "qa_message": {
                "project_id": project_id,
                "initiating_run_id": "live-1",
                "telegram_chat_id": "",
                "deployed_url": "https://exact.example.com",
                "application_id": 42,
                "acceptance_criteria": "the bot answers /start",
                "run_id": run_id,
            },
        },
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    return grant_id


async def _passed_story_with_grant(async_client) -> tuple[str, str, str]:
    project_id, story_id = await _testing_story(async_client)
    run_id = await _qa_run(async_client, project_id, story_id)
    grant_id = await _grant(async_client, project_id, run_id)
    await _settle(async_client, run_id)
    return story_id, run_id, grant_id


async def _escalate(async_client, grant_id: str):
    return await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/escalate", json=ESCALATION
    )


@pytest.mark.asyncio
async def test_escalation_waits_for_the_story_to_route_this_passed_verdict(async_client) -> None:
    story_id, run_id, grant_id = await _passed_story_with_grant(async_client)

    deferred = await _escalate(async_client, grant_id)

    assert deferred.status_code == status.HTTP_409_CONFLICT
    assert deferred.json()["detail"] == QA_ROUTING_PENDING
    grant = (await async_client.get(f"/api/temporary-access-grants/{grant_id}")).json()
    assert grant["escalated_at"] is None
    assert grant["status"] == "granting"

    routed = await async_client.post(
        f"/api/stories/{story_id}/complete", json={"qa_run_id": run_id}
    )
    assert routed.status_code == status.HTTP_200_OK, routed.text
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert run["run_metadata"][QA_ROUTED_KEY]["story_id"] == story_id
    assert run["run_metadata"][QA_ROUTED_KEY]["story_status"] == "completed"

    escalated = await _escalate(async_client, grant_id)

    assert escalated.status_code == status.HTTP_200_OK, escalated.text
    assert escalated.json()["status"] == "revoke_failed"
    assert escalated.json()["escalated_at"] is not None
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert run["status"] == "completed"
    assert run["result"]["qa_outcome"] == "passed"
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == "completed"
    assert story["quarantine_reason"] is None


@pytest.mark.asyncio
async def test_a_status_change_that_names_no_run_is_not_routing(async_client) -> None:
    story_id, _run_id, grant_id = await _passed_story_with_grant(async_client)

    parked = await async_client.post(f"/api/stories/{story_id}/human-review")
    assert parked.status_code == status.HTTP_200_OK, parked.text

    deferred = await _escalate(async_client, grant_id)
    assert deferred.status_code == status.HTTP_409_CONFLICT
    assert deferred.json()["detail"] == QA_ROUTING_PENDING


@pytest.mark.asyncio
async def test_the_stamp_names_only_a_terminal_verdict_of_this_testing_story(async_client) -> None:
    story_id, run_id, _grant_id = await _passed_story_with_grant(async_client)
    other_project, other_story = await _testing_story(async_client)
    other_run = await _qa_run(async_client, other_project, other_story)
    await _settle(async_client, other_run)
    unsettled = await _qa_run(async_client, other_project, other_story)

    foreign = await async_client.post(
        f"/api/stories/{story_id}/complete", json={"qa_run_id": other_run}
    )
    pending = await async_client.post(
        f"/api/stories/{other_story}/start", json={"qa_run_id": unsettled}
    )

    assert foreign.status_code == status.HTTP_409_CONFLICT
    assert pending.status_code == status.HTTP_409_CONFLICT
    # Nothing moved: the refusal is raised inside the transition's transaction.
    for sid in (story_id, other_story):
        assert (await async_client.get(f"/api/stories/{sid}")).json()["status"] == "testing"
    for rid in (run_id, other_run, unsettled):
        run = (await async_client.get(f"/api/runs/{rid}")).json()
        assert QA_ROUTED_KEY not in run["run_metadata"]

    # A story out of TESTING cannot claim a verdict afterwards either.
    parked = await async_client.post(f"/api/stories/{story_id}/human-review")
    assert parked.status_code == status.HTTP_200_OK
    late = await async_client.post(f"/api/stories/{story_id}/start", json={"qa_run_id": run_id})
    assert late.status_code == status.HTTP_409_CONFLICT
    assert (await async_client.get(f"/api/stories/{story_id}")).json()["status"] == (
        "waiting_human_review"
    )
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert QA_ROUTED_KEY not in run["run_metadata"]


@pytest.mark.asyncio
async def test_the_stamp_cannot_be_written_through_the_run_patch(async_client) -> None:
    story_id, run_id, _grant_id = await _passed_story_with_grant(async_client)

    forged = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"run_metadata": {QA_ROUTED_KEY: {"story_id": story_id}}},
    )

    assert forged.status_code == status.HTTP_409_CONFLICT
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert QA_ROUTED_KEY not in run["run_metadata"]


@pytest.mark.asyncio
async def test_a_superseded_verdict_no_longer_holds_the_incident(async_client) -> None:
    """Routing reads the newest QA run only, so an older one is never consumed."""
    story_id, run_id, grant_id = await _passed_story_with_grant(async_client)
    project_id = (await async_client.get(f"/api/stories/{story_id}")).json()["project_id"]
    newer = await _qa_run(async_client, project_id, story_id)

    escalated = await _escalate(async_client, grant_id)

    assert escalated.status_code == status.HTTP_200_OK, escalated.text
    assert escalated.json()["escalated_at"] is not None
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert run["result"]["qa_outcome"] == "passed"
    await _settle(async_client, newer)


@pytest.mark.asyncio
async def test_cleanup_failure_before_any_verdict_still_writes_the_routable_blocker(
    async_client,
) -> None:
    project_id, story_id = await _testing_story(async_client)
    run_id = await _qa_run(async_client, project_id, story_id)
    grant_id = await _grant(async_client, project_id, run_id)

    escalated = await _escalate(async_client, grant_id)

    assert escalated.status_code == status.HTTP_200_OK, escalated.text
    run = (await async_client.get(f"/api/runs/{run_id}")).json()
    assert run["status"] == "failed"
    assert run["result"]["blocker"]["category"] == "qa_cleanup_failed"
    # That verdict is the story's to route, exactly like any other.
    routed = await async_client.post(
        f"/api/stories/{story_id}/human-review", json={"qa_run_id": run_id}
    )
    assert routed.status_code == status.HTTP_200_OK, routed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_escalation_racing_routing_never_commits_before_the_stamp(
    async_client, db_engine, attempt
) -> None:
    """Both writers queue on the QA run row; whichever wins, the invariant holds."""
    story_id, run_id, grant_id = await _passed_story_with_grant(async_client)
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with sessions() as holder:
        await holder.execute(select(Run).where(Run.id == run_id).with_for_update())
        escalation = asyncio.create_task(_escalate(async_client, grant_id))
        routing = asyncio.create_task(
            async_client.post(f"/api/stories/{story_id}/complete", json={"qa_run_id": run_id})
        )
        await asyncio.sleep(0.2)
        assert not escalation.done()
        assert not routing.done()
        await holder.rollback()
    escalated, routed = await asyncio.gather(escalation, routing)

    assert routed.status_code == status.HTTP_200_OK, routed.text
    async with sessions() as session:
        run = await session.get(Run, run_id)
        grant = await session.get(TemporaryAccessGrant, grant_id)
        assert run is not None and grant is not None
        assert run.run_metadata[QA_ROUTED_KEY]["story_id"] == story_id
        assert run.result["qa_outcome"] == "passed"
    if escalated.status_code == status.HTTP_200_OK:
        # It ran after the routing commit and saw the stamp.
        assert grant.escalated_at is not None
    else:
        assert escalated.json()["detail"] == QA_ROUTING_PENDING
        assert grant.escalated_at is None
        again = await _escalate(async_client, grant_id)
        assert again.status_code == status.HTTP_200_OK, again.text
