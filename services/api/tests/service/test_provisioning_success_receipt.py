"""Fresh provisioning on a real database: key first, then phase, then receipt with READY."""

from datetime import UTC, datetime
import uuid

import httpx
import pytest

from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key

COMPLETE = {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE}


async def _discovered(client: httpx.AsyncClient) -> str:
    """The row provider discovery creates: managed, `pending_setup`, no key yet."""
    handle = f"fresh-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": f"{handle}.example.test",
            "public_ip": "203.0.113.40",
            "status": "pending_setup",
        },
    )
    assert created.status_code == httpx.codes.CREATED, created.text
    return handle


async def _reserve(client: httpx.AsyncClient, handle: str) -> dict:
    reserved = await client.post(
        f"/api/servers/{handle}/provisioning-attempts/reserve", json={"max_attempts": 3}
    )
    assert reserved.status_code == httpx.codes.OK, reserved.text
    assert reserved.json()["reserved"] is True
    return reserved.json()


async def _receipt(client: httpx.AsyncClient, handle: str, key: str, **identity) -> dict:
    row = (await client.get(f"/api/servers/{handle}")).json()
    return {
        "profile_version": QA_TARGET_PROFILE_VERSION,
        "proved_at": datetime.now(UTC).isoformat(),
        "identity": {
            "ssh_user": row["ssh_user"],
            "host": row["host"],
            "public_ip": row["public_ip"],
            "ssh_key_fingerprint": normalize_admin_private_key(key).fingerprint,
            **identity,
        },
    }


@pytest.mark.asyncio
async def test_a_keyless_managed_row_cannot_reach_a_complete_phase(async_client):
    handle = await _discovered(async_client)

    refused = await async_client.patch(
        f"/api/servers/{handle}", json={"labels": COMPLETE, "notes": "too early"}
    )
    failed = await async_client.patch(f"/api/servers/{handle}", json={"status": "error"})

    assert refused.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert failed.status_code == httpx.codes.OK, failed.text
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    assert row["labels"].get(PROVISIONING_PHASE_LABEL) is None
    assert row["notes"] is None


@pytest.mark.asyncio
async def test_fresh_provisioning_success_leaves_a_ready_row_with_its_receipt(async_client):
    handle = await _discovered(async_client)
    attempt = await _reserve(async_client, handle)
    key = fleet_private_key()

    stored = await async_client.patch(f"/api/servers/{handle}", json={"ssh_key": key})
    completed = await async_client.patch(f"/api/servers/{handle}", json={"labels": COMPLETE})
    reset = await async_client.post(
        f"/api/servers/{handle}/provisioning-attempts/reset",
        json={
            "attempt_number": attempt["provisioning_attempts"],
            "episode_id": attempt["episode_id"],
            "qa_target_receipt": await _receipt(async_client, handle, key),
        },
    )

    assert stored.status_code == completed.status_code == httpx.codes.OK
    assert reset.status_code == httpx.codes.OK, reset.text
    assert reset.json()["reset"] is True
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    assert row["status"] == "ready"
    assert row["qa_target_version"] == QA_TARGET_PROFILE_VERSION
    assert row["qa_target_proved_at"] is not None


@pytest.mark.asyncio
async def test_a_receipt_for_another_key_leaves_the_episode_open_and_the_row_unproved(
    async_client,
):
    handle = await _discovered(async_client)
    attempt = await _reserve(async_client, handle)
    key = fleet_private_key()
    assert (
        await async_client.patch(f"/api/servers/{handle}", json={"ssh_key": key})
    ).status_code == httpx.codes.OK

    racing = await async_client.post(
        f"/api/servers/{handle}/provisioning-attempts/reset",
        json={
            "attempt_number": attempt["provisioning_attempts"],
            "episode_id": attempt["episode_id"],
            "qa_target_receipt": await _receipt(async_client, handle, fleet_private_key()),
        },
    )

    assert racing.status_code == httpx.codes.CONFLICT
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    # Reserving an attempt does not move the row's status, and a refused receipt
    # closes nothing: the discovered row is exactly as it was.
    assert row["status"] == "pending_setup"
    assert row["qa_target_version"] is None

    # The episode stayed open: the receipt for the key actually stored still
    # closes this same attempt, as READY with its receipt.
    closed = await async_client.post(
        f"/api/servers/{handle}/provisioning-attempts/reset",
        json={
            "attempt_number": attempt["provisioning_attempts"],
            "episode_id": attempt["episode_id"],
            "qa_target_receipt": await _receipt(async_client, handle, key),
        },
    )
    assert closed.status_code == httpx.codes.OK, closed.text
    assert closed.json()["reset"] is True
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    assert row["status"] == "ready"
    assert row["qa_target_version"] == QA_TARGET_PROFILE_VERSION


@pytest.mark.asyncio
async def test_a_superseded_attempt_publishes_no_receipt(async_client):
    handle = await _discovered(async_client)
    old = await _reserve(async_client, handle)
    await _reserve(async_client, handle)
    key = fleet_private_key()
    assert (
        await async_client.patch(f"/api/servers/{handle}", json={"ssh_key": key})
    ).status_code == httpx.codes.OK

    stale = await async_client.post(
        f"/api/servers/{handle}/provisioning-attempts/reset",
        json={
            "attempt_number": old["provisioning_attempts"],
            "episode_id": old["episode_id"],
            "qa_target_receipt": await _receipt(async_client, handle, key),
        },
    )

    assert stale.status_code == httpx.codes.OK, stale.text
    assert stale.json()["reset"] is False
    assert (await async_client.get(f"/api/servers/{handle}")).json()["qa_target_version"] is None
