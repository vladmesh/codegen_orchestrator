"""Managed-target readiness on a real database: key rule, receipt, provenance, races."""

from datetime import UTC, datetime
import uuid

import httpx
import pytest

from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key

ACTIVE = {"detected", "recovering"}


async def _managed_server(client: httpx.AsyncClient, key: str, *, status: str = "ready") -> str:
    handle = f"readiness-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": f"{handle}.example.test",
            "public_ip": "203.0.113.30",
            "ssh_key": key,
            "status": status,
            "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        },
    )
    assert created.status_code == httpx.codes.CREATED, created.text
    assert key.splitlines()[1] not in created.text
    assert created.json()["ssh_key_fingerprint"].startswith("SHA256:")
    assert created.json()["qa_target_version"] is None
    return handle


async def _row(client: httpx.AsyncClient, handle: str) -> dict:
    return (await client.get(f"/api/servers/{handle}")).json()


async def _identity(client: httpx.AsyncClient, handle: str, key: str) -> dict:
    row = await _row(client, handle)
    return {
        "ssh_user": row["ssh_user"],
        "host": row["host"],
        "public_ip": row["public_ip"],
        "ssh_key_fingerprint": normalize_admin_private_key(key).fingerprint,
    }


def _ready(identity: dict) -> dict:
    return {
        "ready": True,
        "profile_version": QA_TARGET_PROFILE_VERSION,
        "proved_at": datetime.now(UTC).isoformat(),
        "identity": identity,
    }


def _failed(identity: dict, phase: str = "admin_login") -> dict:
    return {
        "ready": False,
        "phase": phase,
        "detail": "Timeout after 180s",
        "revision": "d" * 40,
        "identity": identity,
    }


async def _active(client: httpx.AsyncClient, handle: str, incident_type: str) -> list[dict]:
    listed = await client.get(
        "/api/incidents/", params={"server_handle": handle, "incident_type": incident_type}
    )
    assert listed.status_code == httpx.codes.OK, listed.text
    return [row for row in listed.json() if row["status"] in ACTIVE]


@pytest.mark.asyncio
async def test_a_managed_row_is_neither_created_nor_promoted_without_a_key(async_client):
    handle = f"keyless-{uuid.uuid4().hex[:8]}"
    base = {"handle": handle, "host": "keyless.test", "public_ip": "203.0.113.31"}

    refused = await async_client.post("/api/servers/", json=base)
    unmanaged = await async_client.post(
        "/api/servers/", json={**base, "is_managed": False, "status": "ready"}
    )
    promoted = await async_client.patch(f"/api/servers/{handle}", json={"is_managed": True})
    discovered = await async_client.post(
        "/api/servers/",
        json={**base, "handle": f"{handle}-new", "status": "pending_setup"},
    )

    assert refused.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert unmanaged.status_code == httpx.codes.CREATED, unmanaged.text
    assert promoted.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert (await _row(async_client, handle))["is_managed"] is False
    assert discovered.status_code == httpx.codes.CREATED, discovered.text


@pytest.mark.asyncio
async def test_a_refused_key_replacement_leaves_the_stored_row_unchanged(async_client):
    key = fleet_private_key()
    handle = await _managed_server(async_client, key)
    before = await _row(async_client, handle)

    refused = await async_client.patch(
        f"/api/servers/{handle}",
        json={"ssh_key": key.rstrip("\n")[:-40] + "\n", "notes": "rotation attempt"},
    )
    cleared = await async_client.patch(f"/api/servers/{handle}", json={"ssh_key": None})

    assert refused.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert cleared.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    after = await _row(async_client, handle)
    assert after["ssh_key_fingerprint"] == before["ssh_key_fingerprint"]
    assert after["notes"] == before["notes"]
    stored = await async_client.get(f"/api/servers/{handle}/ssh-key")
    assert stored.json()["ssh_key"] == key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"public_ip": "198.51.100.7"}, {"host": "moved.example.test"}, {"ssh_user": "admin"}]
)
async def test_an_identity_change_clears_the_receipt_it_was_proved_for(async_client, change):
    key = fleet_private_key()
    handle = await _managed_server(async_client, key)
    proved = await async_client.post(
        f"/api/servers/{handle}/target-readiness",
        json=_ready(await _identity(async_client, handle, key)),
    )
    assert proved.status_code == httpx.codes.OK, proved.text

    patched = await async_client.patch(f"/api/servers/{handle}", json=change)

    assert patched.status_code == httpx.codes.OK, patched.text
    assert (await _row(async_client, handle))["qa_target_version"] is None


@pytest.mark.asyncio
async def test_a_verdict_proved_before_a_key_change_cannot_land_after_it(async_client):
    """The race the identity fence closes: prove, rotate the key, then report."""
    old_key = fleet_private_key()
    handle = await _managed_server(async_client, old_key)
    old_identity = await _identity(async_client, handle, old_key)

    rotated = await async_client.patch(
        f"/api/servers/{handle}", json={"ssh_key": fleet_private_key()}
    )
    late_ready = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_ready(old_identity)
    )
    late_failure = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_failed(old_identity)
    )

    assert rotated.status_code == httpx.codes.OK, rotated.text
    assert late_ready.status_code == httpx.codes.CONFLICT
    assert late_failure.status_code == httpx.codes.CONFLICT
    row = await _row(async_client, handle)
    assert row["qa_target_version"] is None
    assert row["status"] == "ready"
    assert row["target_readiness_failure_phase"] is None
    assert await _active(async_client, handle, "target_not_ready") == []


@pytest.mark.asyncio
async def test_a_readiness_park_is_its_own_and_is_released_only_by_its_proof(async_client):
    key = fleet_private_key()
    handle = await _managed_server(async_client, key, status="in_use")
    identity = await _identity(async_client, handle, key)

    first = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_failed(identity)
    )
    second = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_failed(identity, "privilege_preflight")
    )

    assert first.status_code == httpx.codes.OK, first.text
    assert second.json()["incident_id"] == first.json()["incident_id"]
    (incident,) = await _active(async_client, handle, "target_not_ready")
    assert incident["details"]["phase"] == "privilege_preflight"
    assert incident["recovery_attempts"] == 1
    parked = await _row(async_client, handle)
    assert parked["status"] == "error"
    assert parked["target_readiness_failure_phase"] == "privilege_preflight"
    assert (await async_client.get(f"/api/servers/{handle}/ssh-key")).json()["ssh_key"] == key

    released = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_ready(identity)
    )

    assert released.status_code == httpx.codes.OK, released.text
    row = await _row(async_client, handle)
    # Back to the lifecycle state the park took, not a generic `ready`.
    assert row["status"] == "in_use"
    assert row["qa_target_version"] == QA_TARGET_PROFILE_VERSION
    assert row["target_readiness_failure_phase"] is None
    assert await _active(async_client, handle, "target_not_ready") == []


@pytest.mark.asyncio
async def test_readiness_never_overwrites_or_clears_an_unrelated_provisioning_failure(
    async_client,
):
    key = fleet_private_key()
    handle = await _managed_server(async_client, key)
    identity = await _identity(async_client, handle, key)
    software = await async_client.post(
        "/api/incidents/provisioning-failure",
        json={
            "server_handle": handle,
            "incident_type": "provisioning_failed",
            "details": {"step": "software_setup", "output": "apt lock"},
        },
    )
    assert software.status_code == httpx.codes.OK, software.text
    errored = await async_client.patch(f"/api/servers/{handle}", json={"status": "error"})
    assert errored.status_code == httpx.codes.OK, errored.text

    failed = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_failed(identity)
    )
    proved = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_ready(identity)
    )

    assert failed.status_code == proved.status_code == httpx.codes.OK
    (provisioning,) = await _active(async_client, handle, "provisioning_failed")
    assert provisioning["id"] == software.json()["id"]
    assert provisioning["details"] == {"step": "software_setup", "output": "apt lock"}
    # QA readiness proved nothing about the software phase: the error stays.
    assert (await _row(async_client, handle))["status"] == "error"


@pytest.mark.asyncio
async def test_a_status_write_during_a_park_takes_its_status_away(async_client):
    """A provisioning failure lands while the target is parked: the proof must not undo it."""
    key = fleet_private_key()
    handle = await _managed_server(async_client, key)
    identity = await _identity(async_client, handle, key)
    parked = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_failed(identity)
    )
    assert parked.status_code == httpx.codes.OK, parked.text

    provisioner = await async_client.patch(f"/api/servers/{handle}", json={"status": "error"})
    proved = await async_client.post(
        f"/api/servers/{handle}/target-readiness", json=_ready(identity)
    )

    assert provisioner.status_code == proved.status_code == httpx.codes.OK
    assert (await _row(async_client, handle))["status"] == "error"


@pytest.mark.asyncio
async def test_a_stale_receipt_is_refused_and_labels_cannot_write_one(async_client):
    key = fleet_private_key()
    handle = await _managed_server(async_client, key)

    stale = await async_client.post(
        f"/api/servers/{handle}/target-readiness",
        json={**_ready(await _identity(async_client, handle, key)), "profile_version": "0" * 16},
    )
    patched = await async_client.patch(
        f"/api/servers/{handle}",
        json={"qa_target_version": QA_TARGET_PROFILE_VERSION, "qa_ssh_user": "qa-observer"},
    )

    assert stale.status_code == httpx.codes.CONFLICT
    assert patched.status_code == httpx.codes.OK
    assert (await _row(async_client, handle))["qa_target_version"] is None
