from unittest.mock import AsyncMock

import httpx
import pytest

from shared.contracts.dto.incident import (
    IncidentCreate,
    IncidentStatus,
    IncidentType,
    IncidentUpdate,
)
from src.clients.api import InfrastructureAPIClient
from src.provisioner.incidents import IncidentPersistenceError, create_incident


@pytest.mark.asyncio
async def test_client_incident_operations_use_typed_dtos(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    client = InfrastructureAPIClient()
    request = AsyncMock(
        side_effect=[
            httpx.Response(201, json=_incident_json(1)),
            httpx.Response(200, json=[]),
            httpx.Response(200, json=_incident_json(1)),
        ]
    )
    monkeypatch.setattr(client, "_request", request)

    created = await client.create_incident(
        IncidentCreate(server_handle="srv-1", incident_type=IncidentType.PROVISIONING_FAILED)
    )
    listed = await client.list_incidents(
        server_handle="srv-1",
        status=IncidentStatus.DETECTED,
        incident_type=IncidentType.PROVISIONING_FAILED,
    )
    updated = await client.update_incident(1, IncidentUpdate(recovery_attempts=2))

    assert created.id == 1
    assert listed == []
    assert updated.id == 1
    assert request.await_args_list[0].args == ("POST", "incidents/")
    assert request.await_args_list[0].kwargs["json"]["incident_type"] == "provisioning_failed"
    assert request.await_args_list[1].kwargs["params"] == {
        "server_handle": "srv-1",
        "status": "detected",
        "incident_type": "provisioning_failed",
    }


@pytest.mark.asyncio
async def test_incident_api_outage_is_explicit_and_diagnostics_are_bounded(monkeypatch):
    record = AsyncMock(side_effect=httpx.ConnectError("api unavailable"))
    monkeypatch.setattr("src.provisioner.incidents.api_client.record_provisioning_failure", record)

    with pytest.raises(IncidentPersistenceError):
        await create_incident(
            "srv-1",
            IncidentType.PROVISIONING_FAILED,
            {"stdout": "x" * 2000, "api_key": "secret", "step": "access_setup"},
        )

    payload = record.await_args.args[0]
    assert payload.incident_type is IncidentType.PROVISIONING_FAILED
    assert payload.details["stdout"].endswith("…")
    assert len(payload.details["stdout"]) <= 513
    assert payload.details["api_key"] == "[redacted]"


@pytest.mark.asyncio
async def test_incident_api_outage_stays_unacked_for_retry(monkeypatch):
    from src.main import process_provisioner_job

    async def _raise_journal_error(self, state):
        raise IncidentPersistenceError("Failed to persist provisioning incident")

    monkeypatch.setattr("src.main.ProvisionerNode.run", _raise_journal_error)

    with pytest.raises(IncidentPersistenceError):
        await process_provisioner_job({"job_id": "job-1", "server_handle": "srv-1"})


def _incident_json(incident_id: int) -> dict:
    return {
        "id": incident_id,
        "server_handle": "srv-1",
        "incident_type": "provisioning_failed",
        "status": "detected",
        "detected_at": "2026-07-13T00:00:00",
        "resolved_at": None,
        "details": {},
        "affected_services": [],
        "recovery_attempts": 0,
        "created_at": "2026-07-13T00:00:00Z",
        "updated_at": "2026-07-13T00:00:00Z",
    }
