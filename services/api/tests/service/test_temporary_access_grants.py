"""The grant record is the thing revocation is driven from, so it must hold.

One live grant per contract slot, a write that survives being repeated, and a
revoke that is not an error the second time.
"""

import asyncio
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models import TemporaryAccessGrant

HEAD_SHA = "b" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"


async def _project_with_run(async_client: AsyncClient) -> tuple[str, str]:
    """Create the project and QA run a grant points at."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"tempaccess_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED

    project = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "Temporary Access", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED

    run_id = f"qa-{uuid.uuid4().hex[:8]}"
    run = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "qa", "project_id": project_id},
    )
    assert run.status_code == status.HTTP_201_CREATED
    return project_id, run_id


def _grant_payload(project_id: str, run_id: str, **overrides) -> dict:
    payload = {
        "id": f"tempaccess-{uuid.uuid4().hex[:8]}",
        "project_id": project_id,
        "env_key": ENV_KEY,
        "subject": "424242",
        "head_sha": HEAD_SHA,
        "qa_run_id": run_id,
        "grant_run_id": f"deploy-grant-{uuid.uuid4().hex[:8]}",
        "qa_message": {
            "story_id": "story-1",
            "project_id": project_id,
            "user_id": "",
            "deployed_url": "https://example.com",
            "application_id": 42,
            "acceptance_criteria": "the bot answers /start",
            "run_id": run_id,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_grant_survives_being_written_twice(async_client: AsyncClient):
    """A caller that crashed after writing must be able to write again."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)

    first = await async_client.post("/api/temporary-access-grants/", json=payload)
    assert first.status_code == status.HTTP_201_CREATED

    second = await async_client.post("/api/temporary-access-grants/", json=payload)
    assert second.status_code == status.HTTP_201_CREATED
    assert second.json()["granted_at"] == first.json()["granted_at"]


@pytest.mark.asyncio
async def test_live_grants_are_readable_without_the_process_that_made_them(
    async_client: AsyncClient,
):
    """The sweep finds the grant from storage alone."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)

    listed = await async_client.get(
        "/api/temporary-access-grants/", params={"live": "true", "project_id": project_id}
    )
    assert listed.status_code == status.HTTP_200_OK
    ids = [row["id"] for row in listed.json()]
    assert payload["id"] in ids


@pytest.mark.asyncio
async def test_one_live_grant_per_contract_slot(async_client: AsyncClient):
    """Two live grants for one value would leave one of them unrevocable."""
    project_id, run_id = await _project_with_run(async_client)
    await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )

    conflict = await async_client.post(
        "/api/temporary-access-grants/",
        json=_grant_payload(project_id, run_id, subject="777"),
    )
    assert conflict.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_revoking_twice_is_not_an_error(async_client: AsyncClient):
    """A retry of a revoke that already landed asks for a state that is true."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"

    first = await async_client.patch(
        grant_url, json={"status": "revoked", "revoke_reason": "run_terminal"}
    )
    assert first.status_code == status.HTTP_200_OK
    assert first.json()["revoked_at"] is not None

    second = await async_client.patch(grant_url, json={"status": "revoked"})
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


@pytest.mark.asyncio
async def test_slot_is_free_again_once_the_access_is_gone(async_client: AsyncClient):
    """A revoked grant does not block the next QA run's access."""
    project_id, run_id = await _project_with_run(async_client)
    first = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=first)
    await async_client.patch(
        f"/api/temporary-access-grants/{first['id']}",
        json={"status": "revoked", "revoke_reason": "run_terminal"},
    )

    again = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    assert again.status_code == status.HTTP_201_CREATED


@pytest.mark.asyncio
async def test_revoked_grant_is_terminal_evidence(async_client: AsyncClient):
    """Nothing reopens a revoked grant."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await async_client.patch(grant_url, json={"status": "revoked", "revoke_reason": "run_terminal"})

    reopened = await async_client.patch(grant_url, json={"status": "granted"})
    assert reopened.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_the_handoff_a_grant_holds_survives_a_restart(async_client: AsyncClient):
    """The QA run is started from the record, so it must come back whole."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)

    stored = await async_client.get(f"/api/temporary-access-grants/{payload['id']}")
    assert stored.status_code == status.HTTP_200_OK
    body = stored.json()
    assert body["status"] == "granting"
    assert body["grant_run_id"] == payload["grant_run_id"]
    assert body["qa_message"]["run_id"] == run_id
    assert body["qa_message"]["deployed_url"] == "https://example.com"
    assert body["qa_dispatched_at"] is None


@pytest.mark.asyncio
async def test_the_grant_for_one_qa_run_is_found_by_that_run(async_client: AsyncClient):
    """The story supervisor asks per run, not per project."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)

    mine = await async_client.get(
        "/api/temporary-access-grants/", params={"live": "true", "qa_run_id": run_id}
    )
    assert [row["id"] for row in mine.json()] == [payload["id"]]

    other = await async_client.get(
        "/api/temporary-access-grants/", params={"live": "true", "qa_run_id": "qa-nobody"}
    )
    assert other.json() == []


@pytest.mark.asyncio
async def test_released_and_escalated_moments_are_stamped_once(async_client: AsyncClient):
    """A repeat after a crash must not move a moment that already happened."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"

    first = await async_client.patch(grant_url, json={"status": "granted", "qa_dispatched": True})
    assert first.status_code == status.HTTP_200_OK
    assert first.json()["qa_dispatched_at"] is not None

    again = await async_client.patch(grant_url, json={"qa_dispatched": True})
    assert again.json()["qa_dispatched_at"] == first.json()["qa_dispatched_at"]

    escalated = await async_client.patch(
        grant_url,
        json={"status": "revoke_failed", "revoke_reason": "run_terminal", "escalated": True},
    )
    assert escalated.json()["escalated_at"] is not None
    repeated = await async_client.patch(grant_url, json={"escalated": True})
    assert repeated.json()["escalated_at"] == escalated.json()["escalated_at"]


def _cleanup_failure(error: str = "revoke deploy deploy-revoke-1 ended failed (give_up)") -> dict:
    return {
        "error": error,
        "run_error_message": f"temporary access {ENV_KEY} is still granted: {error}",
        "run_result": {
            "qa_outcome": "blocked",
            "summary": "temporary test access could not be revoked",
            "blocker": {
                "category": "qa_cleanup_failed",
                "attempted": f"revoke temporary access {ENV_KEY}",
                "sent": f"deploy of {HEAD_SHA} with {ENV_KEY} cleared",
                "received": error,
            },
        },
    }


@pytest.mark.asyncio
async def test_escalation_fails_a_qa_run_that_already_passed(async_client: AsyncClient):
    """The hole this endpoint exists to close.

    The worker inside the QA run finished and recorded `passed` long before the
    revokes ran out. A run that borrowed a test identity has not finished while
    the identity is still admitted, so the cleanup failure is that run's outcome
    — otherwise the story publishes a success with the identity still out.
    """
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    passed = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed", "summary": "it answered"}},
    )
    assert passed.json()["result"]["qa_outcome"] == "passed"

    escalated = await async_client.post(
        f"/api/temporary-access-grants/{grant.json()['id']}/escalate", json=_cleanup_failure()
    )

    assert escalated.status_code == status.HTTP_200_OK
    assert escalated.json()["escalated_at"] is not None
    assert escalated.json()["status"] == "revoke_failed"

    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "failed"
    assert run.json()["result"]["blocker"]["category"] == "qa_cleanup_failed"


@pytest.mark.asyncio
async def test_a_late_worker_verdict_cannot_undo_the_escalation(async_client: AsyncClient):
    """Both orders end in the same place.

    The sweep superseding a passed run is one direction; a worker reporting
    after the sweep already failed the run is the other, and the ordinary run
    patch still refuses it.
    """
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )

    await async_client.post(
        f"/api/temporary-access-grants/{grant.json()['id']}/escalate", json=_cleanup_failure()
    )
    late = await async_client.patch(
        f"/api/runs/{run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed", "summary": "it answered"}},
    )

    assert late.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["result"]["blocker"]["category"] == "qa_cleanup_failed"


@pytest.mark.asyncio
async def test_a_worker_verdict_landing_mid_escalation_still_loses(
    async_client: AsyncClient,
    db_engine,
):
    """The two orders above are sequential; the real one overlaps.

    The escalation and the worker's verdict are decided by different processes at
    the same moment, so "the escalation gets the last word" only holds if it
    takes the rows before it reads them. Here the escalation is held at the grant
    while the worker's pass commits underneath it. Without the lock the two would
    interleave the other way round and the story would read a success on a run
    whose test identity is still admitted.
    """
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    grant_id = grant.json()["id"]
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with sessions() as holder:
        await holder.execute(
            select(TemporaryAccessGrant)
            .where(TemporaryAccessGrant.id == grant_id)
            .with_for_update()
        )

        escalation = asyncio.create_task(
            async_client.post(
                f"/api/temporary-access-grants/{grant_id}/escalate", json=_cleanup_failure()
            )
        )
        # Long enough for the request to reach the endpoint and stop there.
        await asyncio.sleep(1)
        assert not escalation.done(), "the escalation read the grant without taking it"

        passed = await async_client.patch(
            f"/api/runs/{run_id}",
            json={"status": "completed", "result": {"qa_outcome": "passed", "summary": "ok"}},
        )
        assert passed.status_code == status.HTTP_200_OK
        await holder.rollback()

    escalated = await escalation
    assert escalated.status_code == status.HTTP_200_OK

    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] == "failed"
    assert run.json()["result"]["blocker"]["category"] == "qa_cleanup_failed"


@pytest.mark.asyncio
async def test_escalating_twice_is_the_same_state(async_client: AsyncClient):
    """The sweep repeats what it could not confirm; that is not an error."""
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    url = f"/api/temporary-access-grants/{grant.json()['id']}/escalate"

    first = await async_client.post(url, json=_cleanup_failure())
    second = await async_client.post(url, json=_cleanup_failure())

    assert second.status_code == status.HTTP_200_OK
    assert second.json()["escalated_at"] == first.json()["escalated_at"]


@pytest.mark.asyncio
async def test_a_revoked_grant_has_nothing_to_escalate(async_client: AsyncClient):
    """The access went back. Failing the run now would invent a problem."""
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    revoked = await async_client.patch(
        f"/api/temporary-access-grants/{grant.json()['id']}",
        json={"status": "revoked", "revoke_reason": "run_terminal"},
    )
    assert revoked.status_code == status.HTTP_200_OK

    escalated = await async_client.post(
        f"/api/temporary-access-grants/{grant.json()['id']}/escalate", json=_cleanup_failure()
    )

    assert escalated.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] != "failed"
