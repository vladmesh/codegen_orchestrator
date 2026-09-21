"""A reclaimed provisioning delivery replays only its retained finalizer command."""

from datetime import UTC, datetime
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import (
    ProvisioningFinalization,
    ProvisioningFinalizationDisposition,
    QATargetReceipt,
    TargetIdentity,
)
from shared.qa_identity import provisioning_complete_labels
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION, QATargetProof
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src import main as worker
from src.provisioner import handlers

KEY = fleet_private_key()
FINGERPRINT = normalize_admin_private_key(KEY).fingerprint
IDENTITY = TargetIdentity(
    ssh_user="root",
    host="srv-1.example.test",
    public_ip="203.0.113.10",
    ssh_key_fingerprint=FINGERPRINT,
)
COMMAND = ProvisioningFinalization(
    attempt_number=1,
    episode_id="episode-1",
    expected_identity=IDENTITY.model_copy(update={"ssh_key_fingerprint": None}),
    proved_identity=IDENTITY,
    generated_key_fingerprint=FINGERPRINT,
    generated_private_key=KEY,
    complete_labels=provisioning_complete_labels(),
    qa_target_receipt=QATargetReceipt(
        profile_version=QA_TARGET_PROFILE_VERSION,
        proved_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
        identity=IDENTITY,
    ),
)


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.writes: dict[str, list[str]] = {}
        self.expiries: dict[str, int] = {}
        self.hashes: dict[str, dict] = {}
        self.fail_get = False

    async def set(self, key, value, ex):
        self.values[key] = value
        self.writes.setdefault(key, []).append(value)
        self.expiries[key] = ex

    async def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)

    async def hgetall(self, key):
        return self.hashes.get(key, {})


class FakeClient:
    def __init__(self, deliveries=()):
        self.redis = FakeRedis()
        self.deliveries = deliveries
        self.published = []
        self.acked = []
        self.replay_present_at_publish = []
        self.consume_args = None
        self.closed = False

    async def connect(self):
        pass

    async def close(self):
        self.closed = True

    async def consume(self, *args, **kwargs):
        self.consume_args = (args, kwargs)
        for delivery in self.deliveries:
            yield delivery

    async def publish(self, stream, payload):
        self.replay_present_at_publish.append(
            any(key.startswith("provisioner:finalization-replay:") for key in self.redis.values)
        )
        self.published.append((stream, payload))

    async def ack(self, stream, group, message_id):
        self.acked.append(message_id)


def _message(*, reclaimed: bool, message_id: str = "1-0"):
    return SimpleNamespace(
        message_id=message_id,
        reclaimed=reclaimed,
        data={"request_id": "request-1", "server_handle": "srv-1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replay_disposition",
    [
        ProvisioningFinalizationDisposition.IDEMPOTENT,
        ProvisioningFinalizationDisposition.FINALIZED,
    ],
    ids=["committed-response-lost", "not-committed"],
)
async def test_reclaim_replays_the_exact_command_without_running_provisioning_again(
    monkeypatch, replay_disposition
):
    client = FakeClient([_message(reclaimed=False), _message(reclaimed=True)])
    process_calls = []
    finalizations = []

    async def process(job_data, *, retain_finalization):
        process_calls.append(job_data)
        manager = SimpleNamespace(get_private_key=lambda: KEY)
        proof = QATargetProof(
            profile_version=QA_TARGET_PROFILE_VERSION,
            proved_at=COMMAND.qa_target_receipt.proved_at,
            ssh_user=IDENTITY.ssh_user,
            ssh_key_fingerprint=FINGERPRINT,
        )
        return await handlers.handle_provisioning_success(
            "srv-1",
            IDENTITY.public_ip,
            1,
            "episode-1",
            False,
            ssh_manager=manager,
            qa_target_proof=proof,
            expected_identity=COMMAND.expected_identity,
            retain_finalization=retain_finalization,
        )

    async def finalize(server_handle, command):
        finalizations.append((server_handle, command))
        if len(finalizations) == 1:
            raise httpx.ReadTimeout("response lost")
        return replay_disposition

    monkeypatch.setattr(worker, "process_provisioner_job", process)
    monkeypatch.setattr(worker, "finalize_provisioning", finalize)
    monkeypatch.setattr(handlers, "finalize_provisioning", finalize)
    monkeypatch.setattr(worker, "RedisStreamClient", lambda: client)
    monkeypatch.setattr(worker, "validate_provider_policies", lambda: None)
    monkeypatch.setattr(worker, "managed_provider_ids", lambda _provider: frozenset())
    monkeypatch.setattr(worker, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_shutdown", False)

    await worker.run_worker()

    replay_key = worker._finalization_replay_key("1-0")
    ciphertext = client.redis.writes[replay_key][0]
    assert KEY not in ciphertext
    assert client.redis.expiries[replay_key] == worker.FINALIZATION_REPLAY_TTL_SECONDS

    assert len(process_calls) == 1
    assert finalizations == [("srv-1", COMMAND), ("srv-1", COMMAND)]
    assert client.acked == ["1-0"]
    assert client.replay_present_at_publish == [True]
    assert replay_key not in client.redis.values
    assert client.published[-1][1]["status"] == "success"
    assert client.consume_args[1]["claim_pending"] is True
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, "not-fernet-ciphertext"], ids=["expired", "corrupt"])
async def test_missing_or_corrupt_replay_fails_closed_without_running_the_node(monkeypatch, saved):
    client = FakeClient()
    key = worker._finalization_replay_key("1-0")
    if saved is not None:
        client.redis.values[key] = saved
    process = AsyncMock()
    incident = AsyncMock()
    status = AsyncMock()
    monkeypatch.setattr(worker, "process_provisioner_job", process)
    monkeypatch.setattr(worker, "create_incident", incident)
    monkeypatch.setattr(worker, "update_server_status", status, raising=False)

    await worker._handle_stream_message(client, _message(reclaimed=True))

    process.assert_not_awaited()
    status.assert_not_awaited()
    incident.assert_awaited_once()
    details = incident.await_args.args[2]
    assert details["step"] == "finalization_replay"
    assert details["reason"] in {"missing_or_expired", "corrupt"}
    assert KEY not in str(details)
    assert client.acked == ["1-0"]
    assert client.published[-1][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_saved_replay_is_encrypted_and_rejects_another_delivery(monkeypatch):
    client = FakeClient()
    original = _message(reclaimed=False)
    await worker._retain_finalization(client, original, "request-1", "srv-1", COMMAND)
    ciphertext = client.redis.values[worker._finalization_replay_key("1-0")]
    assert COMMAND.generated_private_key not in ciphertext
    assert worker._decode_finalization_replay(ciphertext).finalization == COMMAND

    mismatched = _message(reclaimed=True, message_id="2-0")
    client.redis.values[worker._finalization_replay_key("2-0")] = ciphertext
    incident = AsyncMock()
    monkeypatch.setattr(worker, "create_incident", incident)
    monkeypatch.setattr(worker, "process_provisioner_job", AsyncMock())

    await worker._handle_stream_message(client, mismatched)

    assert incident.await_args.args[2]["reason"] == "command_mismatch"
    assert client.acked == ["2-0"]


@pytest.mark.asyncio
async def test_unavailable_replay_lookup_records_a_typed_failure(monkeypatch):
    client = FakeClient()
    client.redis.fail_get = True
    incident = AsyncMock()
    monkeypatch.setattr(worker, "create_incident", incident)
    monkeypatch.setattr(worker, "process_provisioner_job", AsyncMock())

    await worker._handle_stream_message(client, _message(reclaimed=True))

    incident.assert_awaited_once()
    assert incident.await_args.args[2] == {
        "step": "finalization_replay",
        "reason": "unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [
        ProvisioningFinalizationDisposition.CONFLICT,
        ProvisioningFinalizationDisposition.CONTAINED,
    ],
)
async def test_every_definitive_refusal_cleans_replay_and_records_an_incident(
    monkeypatch, disposition
):
    client = FakeClient()
    msg = _message(reclaimed=True)
    await worker._retain_finalization(client, msg, "request-1", "srv-1", COMMAND)
    incident = AsyncMock()
    monkeypatch.setattr(worker, "create_incident", incident)
    monkeypatch.setattr(worker, "finalize_provisioning", AsyncMock(return_value=disposition))

    await worker._handle_stream_message(client, msg)

    assert worker._finalization_replay_key("1-0") not in client.redis.values
    assert incident.await_args.args[2]["reason"] == disposition.value
    assert client.acked == ["1-0"]
