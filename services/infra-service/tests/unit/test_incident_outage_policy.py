"""Bounded handling for unavailable provisioning incident journal."""

from types import SimpleNamespace

import pytest

from shared.contracts.dto.incident import IncidentType
from shared.contracts.queues.provisioner import ProvisionerMessage
from src.main import _handle_incident_outage, _retry_saved_incident
from src.provisioner.incidents import IncidentPersistenceError


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.values = {}

    async def hincrby(self, key, field, amount):
        fields = self.hashes.setdefault(key, {})
        fields[field] = str(int(fields.get(field, "0")) + amount)
        return int(fields[field])

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def set(self, key, value, ex):
        self.values[key] = value

    async def delete(self, key):
        self.hashes.pop(key, None)


class FakeClient:
    def __init__(self):
        self.redis = FakeRedis()
        self.published = []
        self.acked = []
        self.fail_publish_once = False
        self.fail_ack_once = False

    async def publish(self, stream, payload):
        if self.fail_publish_once:
            self.fail_publish_once = False
            raise RuntimeError("redis publish unavailable")
        self.published.append((stream, payload))

    async def ack(self, stream, group, message_id):
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("redis ack unavailable")
        self.acked.append(message_id)


@pytest.mark.asyncio
async def test_outage_budget_emits_one_terminal_result_then_acks():
    client = FakeClient()
    msg = SimpleNamespace(message_id="1-0")
    job = ProvisionerMessage(request_id="request-1", server_handle="srv-1")
    error = IncidentPersistenceError("srv-1", {"step": "access"})

    await _handle_incident_outage(client, msg, job, error)
    await _handle_incident_outage(client, msg, job, error)
    assert client.published == []
    assert client.acked == []

    await _handle_incident_outage(client, msg, job, error)
    assert len(client.published) == 1
    assert client.published[0][1]["status"] == "failed"
    assert client.acked == ["1-0"]

    await _handle_incident_outage(client, msg, job, error)
    assert len(client.published) == 1


@pytest.mark.asyncio
async def test_terminal_publish_failure_leaves_entry_unacked_for_a_single_retry():
    client = FakeClient()
    client.fail_publish_once = True
    msg = SimpleNamespace(message_id="publish-failure")
    job = ProvisionerMessage(request_id="request-publish", server_handle="srv-1")
    error = IncidentPersistenceError("srv-1", {"step": "access"})

    await _handle_incident_outage(client, msg, job, error)
    await _handle_incident_outage(client, msg, job, error)
    with pytest.raises(RuntimeError, match="publish unavailable"):
        await _handle_incident_outage(client, msg, job, error)

    assert client.published == []
    assert client.acked == []

    await _handle_incident_outage(client, msg, job, error)

    assert len(client.published) == 1
    assert client.acked == ["publish-failure"]


@pytest.mark.asyncio
async def test_terminal_ack_failure_retries_ack_without_second_terminal_publish():
    client = FakeClient()
    client.fail_ack_once = True
    msg = SimpleNamespace(message_id="ack-failure")
    job = ProvisionerMessage(request_id="request-ack", server_handle="srv-1")
    error = IncidentPersistenceError("srv-1", {"step": "access"})

    await _handle_incident_outage(client, msg, job, error)
    await _handle_incident_outage(client, msg, job, error)
    with pytest.raises(RuntimeError, match="ack unavailable"):
        await _handle_incident_outage(client, msg, job, error)

    assert len(client.published) == 1
    assert client.acked == []

    await _handle_incident_outage(client, msg, job, error)

    assert len(client.published) == 1
    assert client.acked == ["ack-failure"]


@pytest.mark.asyncio
async def test_reclaimed_journal_retry_does_not_repeat_provisioning(monkeypatch):
    client = FakeClient()
    msg = SimpleNamespace(message_id="2-0")
    job = ProvisionerMessage(request_id="request-2", server_handle="srv-1")
    state = {"server_handle": "srv-1", "details": '{"step": "access"}'}
    recorded = []

    async def record(server_handle, incident_type, details):
        recorded.append((server_handle, incident_type, details))

    monkeypatch.setattr("src.main.create_incident", record)
    handled = await _retry_saved_incident(client, msg, job, state)

    assert handled is True
    assert recorded == [("srv-1", IncidentType.PROVISIONING_FAILED, {"step": "access"})]
    assert len(client.published) == 1
    assert client.acked == ["2-0"]
