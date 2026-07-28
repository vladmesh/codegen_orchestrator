"""The grant record is the thing revocation is driven from, so it must hold.

One live grant per contract slot, a write that survives being repeated, and a
revoke that is not an error the second time.
"""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest

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
