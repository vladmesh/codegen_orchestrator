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

    async def publish(self, stream, payload):
        self.published.append((stream, payload))

    async def ack(self, stream, group, message_id):
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
