"""The grant record is the thing revocation is driven from, so it must hold.

One live grant per contract slot, a write that survives being repeated, and a
record that closes only on readings of the server the access was handed out on.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.contracts.dto.temporary_access import (
    REVOKE_CONFIRMATION_READINGS,
    REVOKE_CONFIRMATION_WINDOW,
)
from shared.models import TemporaryAccessGrant

HEAD_SHA = "b" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"
APPLICATION_ID = 42


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
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "Temporary Access",
            "config": {},
        },
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
            "initiating_run_id": "live-1",
            "telegram_chat_id": "",
            "deployed_url": "https://example.com",
            "application_id": 42,
            "acceptance_criteria": "the bot answers /start",
            "run_id": run_id,
        },
    }
    payload.update(overrides)
    return payload


def _reading(present: bool, *, observation_id: str, **overrides) -> dict:
    """One reading of the running service, as the reconciler reports it."""
    payload = {
        "observation_id": observation_id,
        "application_id": APPLICATION_ID,
        "server_handle": "vps-1",
        "service_slug": "palindrome-bot",
        "env_key": ENV_KEY,
        "present": present,
        "containers": 2,
    }
    payload.update(overrides)
    return payload


async def _start_revoking(async_client: AsyncClient, grant_id: str) -> None:
    await async_client.patch(
        f"/api/temporary-access-grants/{grant_id}",
        json={
            "status": "revoking",
            "revoke_reason": "run_terminal",
            "revoke_run_id": "deploy-revoke-1",
        },
    )


async def _age_the_clear_streak(db_engine, grant_id: str) -> None:
    """Put the first empty reading far enough back for the window to have passed.

    The confirmation window is real time, and a test must not wait it out.
    """
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        await session.execute(
            update(TemporaryAccessGrant)
            .where(TemporaryAccessGrant.id == grant_id)
            .values(slot_clear_since=datetime.now(UTC) - REVOKE_CONFIRMATION_WINDOW - timedelta(1))
        )
        await session.commit()


async def _revoke_by_observation(async_client: AsyncClient, db_engine, grant_id: str):
    """Take a grant to revoked the only way there is: readings that agree."""
    await _start_revoking(async_client, grant_id)
    for reading in range(REVOKE_CONFIRMATION_READINGS - 1):
        answer = await async_client.post(
            f"/api/temporary-access-grants/{grant_id}/observation",
            json=_reading(False, observation_id=f"envobs-deploy-revoke-1-{reading}"),
        )
        assert answer.status_code == status.HTTP_200_OK
    await _age_the_clear_streak(db_engine, grant_id)
    return await async_client.post(
        f"/api/temporary-access-grants/{grant_id}/observation",
        json=_reading(
            False, observation_id=f"envobs-deploy-revoke-1-{REVOKE_CONFIRMATION_READINGS - 1}"
        ),
    )


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
async def test_nothing_may_declare_a_grant_revoked(async_client: AsyncClient):
    """The hard invariant: no caller can assert what the deployed service holds.

    Between the deploy that clears the value and the running service stands
    GitHub Actions, so a caller saying "revoked" is saying it believes an effect
    it cannot see. The record only accepts readings of the server.
    """
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"

    declared = await async_client.patch(
        grant_url, json={"status": "revoked", "revoke_reason": "run_terminal"}
    )

    assert declared.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    stored = await async_client.get(grant_url)
    assert stored.json()["status"] == "granting"
    assert stored.json()["revoked_at"] is None

    listed = await async_client.get(
        "/api/temporary-access-grants/", params={"live": "true", "project_id": project_id}
    )
    assert payload["id"] in [row["id"] for row in listed.json()]


@pytest.mark.asyncio
async def test_readings_that_agree_over_the_window_close_the_grant(
    async_client: AsyncClient, db_engine
):
    """The one way in: the server was read, more than once, and it stayed empty."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)

    revoked = await _revoke_by_observation(async_client, db_engine, payload["id"])

    assert revoked.status_code == status.HTTP_200_OK
    body = revoked.json()
    assert body["status"] == "revoked"
    assert body["revoked_at"] is not None
    assert body["revoke_reason"] == "run_terminal"
    assert body["slot_clear_readings"] >= REVOKE_CONFIRMATION_READINGS


@pytest.mark.asyncio
async def test_one_clear_reading_does_not_end_reconciliation(async_client: AsyncClient):
    """A reading is a moment, and a dispatch already in flight lands after moments.

    So the grant stays live and readable by the sweep, which is what gives the
    next cycle something to correct.
    """
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    await _start_revoking(async_client, payload["id"])

    answer = await async_client.post(
        f"/api/temporary-access-grants/{payload['id']}/observation",
        json=_reading(False, observation_id="envobs-deploy-revoke-1-0"),
    )

    assert answer.status_code == status.HTTP_200_OK
    assert answer.json()["status"] == "revoking"
    assert answer.json()["slot_clear_readings"] == 1
    live = await async_client.get(
        "/api/temporary-access-grants/", params={"live": "true", "project_id": project_id}
    )
    assert payload["id"] in [row["id"] for row in live.json()]


@pytest.mark.asyncio
async def test_a_value_seen_again_restarts_the_confirmation(async_client: AsyncClient, db_engine):
    """A late writer during the window is caught, and the streak starts over."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await _start_revoking(async_client, payload["id"])

    await async_client.post(
        f"{grant_url}/observation", json=_reading(False, observation_id="envobs-deploy-revoke-1-0")
    )
    await _age_the_clear_streak(db_engine, payload["id"])
    back = await async_client.post(
        f"{grant_url}/observation", json=_reading(True, observation_id="envobs-deploy-revoke-1-1")
    )

    assert back.json()["status"] == "revoking"
    assert back.json()["slot_clear_readings"] == 0
    assert back.json()["slot_clear_since"] is None


@pytest.mark.asyncio
async def test_a_reading_of_another_application_is_refused(async_client: AsyncClient):
    """A project may run on several servers; only one ran the bot QA tested."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    await _start_revoking(async_client, payload["id"])

    elsewhere = await async_client.post(
        f"/api/temporary-access-grants/{payload['id']}/observation",
        json=_reading(
            False, observation_id="envobs-deploy-revoke-1-0", application_id=APPLICATION_ID + 1
        ),
    )

    assert elsewhere.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    stored = await async_client.get(f"/api/temporary-access-grants/{payload['id']}")
    assert stored.json()["slot_clear_readings"] == 0


@pytest.mark.asyncio
async def test_the_same_reading_delivered_twice_counts_once(async_client: AsyncClient, db_engine):
    """A reconciler repeating what it could not confirm must not confirm itself."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await _start_revoking(async_client, payload["id"])
    await _age_the_clear_streak(db_engine, payload["id"])

    first = await async_client.post(
        f"{grant_url}/observation", json=_reading(False, observation_id="envobs-deploy-revoke-1-0")
    )
    await _age_the_clear_streak(db_engine, payload["id"])
    again = await async_client.post(
        f"{grant_url}/observation", json=_reading(False, observation_id="envobs-deploy-revoke-1-0")
    )

    assert first.json()["slot_clear_readings"] == 1
    assert again.json()["slot_clear_readings"] == 1
    assert again.json()["status"] == "revoking"


@pytest.mark.asyncio
async def test_slot_is_free_again_once_the_access_is_gone(async_client: AsyncClient, db_engine):
    """A revoked grant does not block the next QA run's access."""
    project_id, run_id = await _project_with_run(async_client)
    first = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=first)
    await _revoke_by_observation(async_client, db_engine, first["id"])

    again = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    assert again.status_code == status.HTTP_201_CREATED


@pytest.mark.asyncio
async def test_no_caller_reopens_a_revoked_grant_by_hand(async_client: AsyncClient, db_engine):
    """A closed grant is evidence, and an opinion does not move it."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await _revoke_by_observation(async_client, db_engine, payload["id"])

    reopened = await async_client.patch(grant_url, json={"status": "granted"})
    assert reopened.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_a_value_read_after_the_grant_closed_puts_it_back_under_reconciliation(
    async_client: AsyncClient, db_engine
):
    """The reviewed hole: the readings stopped mattering the moment they agreed.

    A dispatch GitHub Actions had already accepted lands after the confirmation,
    and the record was closed with nothing left watching the slot. So a reading
    is still taken against a closed grant, and one that finds the value reopens
    it — with its own retry budget, because this is a new disagreement rather
    than the one that was already settled.
    """
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await _revoke_by_observation(async_client, db_engine, payload["id"])

    late = await async_client.post(
        f"{grant_url}/observation", json=_reading(True, observation_id="envobs-deploy-revoke-1-9")
    )

    assert late.status_code == status.HTTP_200_OK
    body = late.json()
    assert body["status"] == "revoking"
    assert body["revoke_reason"] == "observed_after_revoke"
    assert body["revoked_at"] is None
    assert body["reopened_at"] is not None
    assert body["revoke_attempts"] == 0
    assert body["slot_clear_readings"] == 0
    assert ENV_KEY in body["last_error"]


@pytest.mark.asyncio
async def test_an_empty_slot_read_after_the_grant_closed_leaves_it_closed(
    async_client: AsyncClient, db_engine
):
    """The watch that catches a returned value must not disturb the ordinary case."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    grant_url = f"/api/temporary-access-grants/{payload['id']}"
    await _revoke_by_observation(async_client, db_engine, payload["id"])

    again = await async_client.post(
        f"{grant_url}/observation", json=_reading(False, observation_id="envobs-deploy-revoke-1-9")
    )

    assert again.status_code == status.HTTP_200_OK
    body = again.json()
    assert body["status"] == "revoked"
    assert body["reopened_at"] is None
    # Each reading of a closed slot is its own question, so the count moves on.
    assert body["slot_clear_readings"] == REVOKE_CONFIRMATION_READINGS + 1
    assert body["observation_id"] == "envobs-deploy-revoke-1-9"


@pytest.mark.asyncio
async def test_a_closed_grant_does_not_take_back_a_later_grants_slot(
    async_client: AsyncClient, db_engine
):
    """The slot holds one value, and a live grant owns it.

    Reopening the closed grant here would revoke the access the next QA run is
    using: what the reading found is that grant's value, not this one's leftover.
    """
    project_id, run_id = await _project_with_run(async_client)
    closed = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=closed)
    await _revoke_by_observation(async_client, db_engine, closed["id"])

    successor = _grant_payload(project_id, run_id)
    made = await async_client.post("/api/temporary-access-grants/", json=successor)
    assert made.status_code == status.HTTP_201_CREATED

    late = await async_client.post(
        f"/api/temporary-access-grants/{closed['id']}/observation",
        json=_reading(True, observation_id="envobs-deploy-revoke-1-9"),
    )

    assert late.status_code == status.HTTP_200_OK
    assert late.json()["status"] == "revoked"
    still_live = await async_client.get(f"/api/temporary-access-grants/{successor['id']}")
    assert still_live.json()["status"] == "granting"


@pytest.mark.asyncio
async def test_the_sweep_reads_recently_closed_grants_and_forgets_older_ones(
    async_client: AsyncClient, db_engine
):
    """The watch is bounded: the sweep names how far back a closed slot is read."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    await _revoke_by_observation(async_client, db_engine, payload["id"])

    watched = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
    )
    assert [row["id"] for row in watched.json()] == [payload["id"]]

    forgotten = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        },
    )
    assert forgotten.json() == []


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
async def test_escalation_preserves_a_qa_run_that_already_passed(async_client: AsyncClient):
    """The grant records its incident without replacing an earlier QA verdict."""
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
    assert run.json()["status"] == "completed"
    assert run.json()["result"]["qa_outcome"] == "passed"
    assert run.json()["completed_at"] is not None


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
async def test_a_worker_verdict_landing_mid_escalation_keeps_the_first_outcome(
    async_client: AsyncClient,
    db_engine,
):
    """The escalation cannot replace a verdict committed while it waited."""
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
    assert run.json()["status"] == "completed"
    assert run.json()["result"]["qa_outcome"] == "passed"


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
async def test_a_revoked_grant_has_nothing_to_escalate(async_client: AsyncClient, db_engine):
    """The access went back. Failing the run now would invent a problem."""
    project_id, run_id = await _project_with_run(async_client)
    grant = await async_client.post(
        "/api/temporary-access-grants/", json=_grant_payload(project_id, run_id)
    )
    revoked = await _revoke_by_observation(async_client, db_engine, grant.json()["id"])
    assert revoked.json()["status"] == "revoked"

    escalated = await async_client.post(
        f"/api/temporary-access-grants/{grant.json()['id']}/escalate", json=_cleanup_failure()
    )

    assert escalated.status_code == status.HTTP_409_CONFLICT
    run = await async_client.get(f"/api/runs/{run_id}")
    assert run.json()["status"] != "failed"


async def _age_the_slot(db_engine, grant_id: str, *, days: int) -> None:
    """Put a closed grant far enough back that only the slow check would find it.

    The fast watch is bounded in minutes and a test must not wait it out; what is
    being checked here is the level that runs when that one has long stopped.
    """
    long_ago = datetime.now(UTC) - timedelta(days=days)
    sessions = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        await session.execute(
            update(TemporaryAccessGrant)
            .where(TemporaryAccessGrant.id == grant_id)
            .values(granted_at=long_ago, revoked_at=long_ago, observed_at=long_ago)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_slot_the_watch_has_forgotten_comes_back_for_the_slow_check(
    async_client: AsyncClient, db_engine
):
    """The reviewed hole: past the watch, nothing read the slot ever again.

    A value written back at minute 61 stood for good. The contract still says the
    key is empty while no grant holds it, so the slot returns on its own cadence
    however long ago the grant closed.
    """
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    await _revoke_by_observation(async_client, db_engine, payload["id"])
    await _age_the_slot(db_engine, payload["id"], days=9)

    forgotten_by_the_watch = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": datetime.now(UTC).isoformat(),
        },
    )
    assert forgotten_by_the_watch.json() == []

    due = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": datetime.now(UTC).isoformat(),
            "slot_audit_before": (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        },
    )
    assert [row["id"] for row in due.json()] == [payload["id"]]


@pytest.mark.asyncio
async def test_a_slot_read_within_the_interval_is_not_asked_for_again(
    async_client: AsyncClient, db_engine
):
    """One ssh per slot per interval: a slot just read is not due."""
    project_id, run_id = await _project_with_run(async_client)
    payload = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=payload)
    await _revoke_by_observation(async_client, db_engine, payload["id"])

    due = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": datetime.now(UTC).isoformat(),
            "slot_audit_before": (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        },
    )

    assert due.json() == []


@pytest.mark.asyncio
async def test_the_slow_check_reads_a_slot_once_however_many_grants_it_held(
    async_client: AsyncClient, db_engine
):
    """The slot holds one value, so checking it is one reading, not one per grant.

    Every grant a project ever made for the key is closed, and the one that
    answers for the slot now is the newest. Returning the older ones would be the
    same ssh repeated for history.
    """
    project_id, run_id = await _project_with_run(async_client)
    first = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=first)
    await _revoke_by_observation(async_client, db_engine, first["id"])
    await _age_the_slot(db_engine, first["id"], days=30)

    second = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=second)
    await _revoke_by_observation(async_client, db_engine, second["id"])
    await _age_the_slot(db_engine, second["id"], days=9)

    due = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": datetime.now(UTC).isoformat(),
            "slot_audit_before": (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        },
    )

    assert [row["id"] for row in due.json()] == [second["id"]]


@pytest.mark.asyncio
async def test_a_live_grant_is_not_also_a_slot_the_slow_check_reads(
    async_client: AsyncClient, db_engine
):
    """A slot with an owner is reconciled by that owner, on the fast cadence."""
    project_id, run_id = await _project_with_run(async_client)
    closed = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=closed)
    await _revoke_by_observation(async_client, db_engine, closed["id"])
    await _age_the_slot(db_engine, closed["id"], days=9)

    live = _grant_payload(project_id, run_id)
    await async_client.post("/api/temporary-access-grants/", json=live)

    due = await async_client.get(
        "/api/temporary-access-grants/",
        params={
            "live": "true",
            "project_id": project_id,
            "revoked_after": datetime.now(UTC).isoformat(),
            "slot_audit_before": (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        },
    )

    assert [row["id"] for row in due.json()] == [live["id"]]
