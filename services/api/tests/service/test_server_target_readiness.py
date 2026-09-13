"""Managed-target readiness on a real database: key boundary, receipt, incident, idempotence."""

from datetime import UTC, datetime
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import httpx
import pytest

from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL


def _fleet_key() -> str:
    text = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return text if text.endswith("\n") else text + "\n"


async def _managed_server(client: httpx.AsyncClient, key: str) -> str:
    handle = f"readiness-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": "203.0.113.30",
            "public_ip": "203.0.113.30",
            "ssh_key": key,
            "status": "ready",
            "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        },
    )
    assert created.status_code == httpx.codes.CREATED, created.text
    assert key.splitlines()[1] not in created.text
    assert created.json()["ssh_key_fingerprint"].startswith("SHA256:")
    assert created.json()["qa_target_version"] is None
    return handle


async def _active_readiness_incidents(client: httpx.AsyncClient, handle: str) -> list[dict]:
    listed = await client.get(
        "/api/incidents/",
        params={"server_handle": handle, "incident_type": "provisioning_failed"},
    )
    assert listed.status_code == httpx.codes.OK, listed.text
    return [row for row in listed.json() if row["status"] in {"detected", "recovering"}]


@pytest.mark.asyncio
async def test_a_refused_key_replacement_leaves_the_stored_row_unchanged(async_client):
    key = _fleet_key()
    handle = await _managed_server(async_client, key)
    before = (await async_client.get(f"/api/servers/{handle}")).json()

    refused = await async_client.patch(
        f"/api/servers/{handle}",
        json={"ssh_key": key.rstrip("\n")[:-40] + "\n", "notes": "rotation attempt"},
    )

    assert refused.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    after = (await async_client.get(f"/api/servers/{handle}")).json()
    assert after["ssh_key_fingerprint"] == before["ssh_key_fingerprint"]
    assert after["notes"] == before["notes"]
    stored = await async_client.get(f"/api/servers/{handle}/ssh-key")
    assert stored.json()["ssh_key"] == key


@pytest.mark.asyncio
async def test_a_failed_target_is_parked_once_and_a_proof_releases_it(async_client):
    key = _fleet_key()
    handle = await _managed_server(async_client, key)
    failure = {
        "ready": False,
        "phase": "admin_login",
        "detail": "UNREACHABLE!",
        "revision": "d" * 40,
    }

    first = await async_client.post(f"/api/servers/{handle}/target-readiness", json=failure)
    second = await async_client.post(f"/api/servers/{handle}/target-readiness", json=failure)

    assert first.status_code == httpx.codes.OK, first.text
    assert second.json()["incident_id"] == first.json()["incident_id"]
    incidents = await _active_readiness_incidents(async_client, handle)
    assert len(incidents) == 1
    assert incidents[0]["details"]["step"] == "target_readiness"
    assert incidents[0]["details"]["phase"] == "admin_login"
    assert incidents[0]["details"]["revision"] == "d" * 40
    parked = (await async_client.get(f"/api/servers/{handle}")).json()
    assert parked["status"] == "error"
    assert parked["qa_target_version"] is None
    # The evidence an operator repairs from is still there.
    assert (await async_client.get(f"/api/servers/{handle}/ssh-key")).json()["ssh_key"] == key

    proved = await async_client.post(
        f"/api/servers/{handle}/target-readiness",
        json={
            "ready": True,
            "profile_version": QA_TARGET_PROFILE_VERSION,
            "proved_at": datetime.now(UTC).isoformat(),
        },
    )

    assert proved.status_code == httpx.codes.OK, proved.text
    released = (await async_client.get(f"/api/servers/{handle}")).json()
    assert released["status"] == "ready"
    assert released["qa_target_version"] == QA_TARGET_PROFILE_VERSION
    assert await _active_readiness_incidents(async_client, handle) == []


@pytest.mark.asyncio
async def test_a_stale_receipt_is_refused_and_labels_cannot_write_one(async_client):
    handle = await _managed_server(async_client, _fleet_key())

    stale = await async_client.post(
        f"/api/servers/{handle}/target-readiness",
        json={
            "ready": True,
            "profile_version": "0123456789abcdef",
            "proved_at": datetime.now(UTC).isoformat(),
        },
    )
    patched = await async_client.patch(
        f"/api/servers/{handle}",
        json={"qa_target_version": QA_TARGET_PROFILE_VERSION, "qa_ssh_user": "qa-observer"},
    )

    assert stale.status_code == httpx.codes.CONFLICT
    assert patched.status_code == httpx.codes.OK
    assert (await async_client.get(f"/api/servers/{handle}")).json()["qa_target_version"] is None
