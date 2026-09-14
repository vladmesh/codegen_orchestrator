"""Real-database coverage for the server-row provisioning finalizer."""

import asyncio
from datetime import UTC, datetime
import uuid

import httpx
import pytest

from shared.qa_identity import provisioning_complete_labels
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key


async def _discovered(client: httpx.AsyncClient) -> str:
    handle = f"fresh-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": f"{handle}.example.test",
            "public_ip": "203.0.113.40",
            "status": "pending_setup",
        },
    )
    assert response.status_code == httpx.codes.CREATED, response.text
    return handle


async def _reserve(client: httpx.AsyncClient, handle: str) -> dict:
    response = await client.post(
        f"/api/servers/{handle}/provisioning-attempts/reserve", json={"max_attempts": 3}
    )
    assert response.status_code == httpx.codes.OK, response.text
    return response.json()


async def _command(client: httpx.AsyncClient, handle: str, key: str) -> dict:
    row = (await client.get(f"/api/servers/{handle}")).json()
    attempt = await _reserve(client, handle)
    expected = {
        "ssh_user": row["ssh_user"],
        "host": row["host"],
        "public_ip": row["public_ip"],
        "ssh_key_fingerprint": row["ssh_key_fingerprint"],
    }
    proved = expected | {"ssh_key_fingerprint": normalize_admin_private_key(key).fingerprint}
    receipt = {
        "profile_version": QA_TARGET_PROFILE_VERSION,
        "proved_at": datetime.now(UTC).isoformat(),
        "identity": proved,
    }
    return {
        "attempt_number": attempt["provisioning_attempts"],
        "episode_id": attempt["episode_id"],
        "expected_identity": expected,
        "proved_identity": proved,
        "generated_key_fingerprint": proved["ssh_key_fingerprint"],
        "generated_private_key": key,
        "complete_labels": provisioning_complete_labels(),
        "qa_target_receipt": receipt,
    }


async def _finalize(client: httpx.AsyncClient, handle: str, command: dict) -> httpx.Response:
    return await client.post(f"/api/servers/{handle}/provisioning/finalize", json=command)


@pytest.mark.asyncio
async def test_success_commits_the_exact_key_labels_receipt_reset_and_ready(async_client):
    handle = await _discovered(async_client)
    key = fleet_private_key()
    command = await _command(async_client, handle, key)

    response = await _finalize(async_client, handle, command)

    assert response.status_code == httpx.codes.OK, response.text
    assert response.json()["disposition"] == "finalized"
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    stored = (await async_client.get(f"/api/servers/{handle}/ssh-key")).json()["ssh_key"]
    assert stored == normalize_admin_private_key(key).text
    assert row["ssh_key_fingerprint"] == command["generated_key_fingerprint"]
    assert row["labels"] | provisioning_complete_labels() == row["labels"]
    assert row["qa_target_version"] == QA_TARGET_PROFILE_VERSION
    assert row["status"] == "ready"
    assert row["provisioning_attempts"] == 0
    assert row["provisioning_episode_id"] is None


@pytest.mark.asyncio
async def test_exact_duplicate_is_idempotent_but_changed_duplicate_conflicts(async_client):
    handle = await _discovered(async_client)
    command = await _command(async_client, handle, fleet_private_key())
    assert (await _finalize(async_client, handle, command)).json()["disposition"] == "finalized"

    duplicate = await _finalize(async_client, handle, command)
    changed = command | {"complete_labels": {"provisioning_phase": "complete"}}
    conflict = await _finalize(async_client, handle, changed)

    assert duplicate.json()["disposition"] == "idempotent"
    assert conflict.json() == {
        "disposition": "conflict",
        "reason": "finalized_delivery_mismatch",
        "provisioning_attempts": 0,
        "episode_id": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ssh_key", fleet_private_key()),
        ("ssh_user", "operator"),
        ("host", "operator.example.test"),
        ("public_ip", "198.51.100.20"),
    ],
)
async def test_operator_identity_edit_races_as_one_serial_transaction(
    async_client, field, replacement
):
    handle = await _discovered(async_client)
    command = await _command(async_client, handle, fleet_private_key())

    finalized, edited = await asyncio.gather(
        _finalize(async_client, handle, command),
        async_client.patch(f"/api/servers/{handle}", json={field: replacement}),
    )

    assert edited.status_code == httpx.codes.OK, edited.text
    row = (await async_client.get(f"/api/servers/{handle}")).json()
    if finalized.json()["disposition"] == "finalized":
        assert row["status"] == "ready"
        assert row["qa_target_version"] is None
    else:
        assert finalized.json()["disposition"] == "conflict"
        assert row["status"] == "pending_setup"
        assert row["qa_target_version"] is None
        assert "provisioning_phase" not in row["labels"]


@pytest.mark.asyncio
async def test_newer_attempt_races_finalization_without_a_mixed_row(async_client):
    handle = await _discovered(async_client)
    command = await _command(async_client, handle, fleet_private_key())

    finalized, reserved = await asyncio.gather(
        _finalize(async_client, handle, command),
        _reserve(async_client, handle),
    )

    row = (await async_client.get(f"/api/servers/{handle}")).json()
    if finalized.json()["disposition"] == "finalized":
        assert reserved["provisioning_attempts"] == 1
        assert reserved["episode_id"] != command["episode_id"]
        assert row["status"] == "ready"
        assert row["qa_target_version"] == QA_TARGET_PROFILE_VERSION
    else:
        assert finalized.json()["disposition"] == "conflict"
        assert reserved["provisioning_attempts"] == 2
        assert row["qa_target_version"] is None
        assert "provisioning_phase" not in row["labels"]
