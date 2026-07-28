import httpx
import pytest

from shared.clients.time4vps import Time4VPSAPIError
from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import ServerStatus
from src.tasks import server_sync


@pytest.mark.asyncio
async def test_server_sync_integration_flow(time4vps_mock, api_client):
    """
    Integration Test: Server Sync Flow

    1. Seed Time4VPS API Key in API service.
    2. Mock Time4VPS Server List.
    3. Run sync task.
    4. Verify Server created in API.
    """
    # 1. Seed API Key
    # We use a raw request because SchedulerAPIClient doesn't support creating keys (by design)
    async with httpx.AsyncClient(base_url=api_client.base_url) as client:
        resp = await client.post(
            "/api/api-keys/",
            json={
                "service": "time4vps",
                "type": "credentials",
                "value": {"username": "test", "password": "test"},
                "project_id": None,
            },
        )
        assert resp.status_code == httpx.codes.CREATED, f"Failed to seed API key: {resp.text}"

    # 2. Mock Time4VPS Response
    time4vps_mock.get("/api/server").respond(
        status_code=200,
        json=[
            {
                "id": 999,
                "domain": "integration-vps.com",
                "ip": "10.0.0.1",
                "price": "9.99",
                "status": "Active",  # Time4VPS returns capitalized
            }
        ],
    )
    # Mock Details call (sync fetches details too)
    time4vps_mock.get("/api/server/999").respond(
        status_code=200,
        json={
            "server": {
                "id": 999,
                "domain": "integration-vps.com",
                "ip": "10.0.0.1",
                "status": "Active",
                "specs": {
                    "os": "Ubuntu 22.04",
                    "cpu": "2",
                    "ram": "4096",
                    "disk": "40960",  # 40GB
                },
                "usage": {"disk_usage": "1024"},
            }
        },
    )

    # 3. Run Sync Task
    # sync_servers returns (discovered, updated, missing)
    # We call internal method as worker is infinite loop
    time4vps_client = await server_sync.get_time4vps_client()
    d, u, m = await server_sync._sync_server_list(time4vps_client)

    assert d == 1  # 1 discovered

    # 4. Verify in API
    servers = await api_client.get_servers()
    target = next((s for s in servers if s.public_ip == "10.0.0.1"), None)

    assert target is not None
    assert target.handle == "vps-999"
    assert target.status == ServerStatus.PENDING_SETUP  # New managed servers are pending setup
    assert target.is_managed is True


@pytest.mark.asyncio
async def test_provider_outage_is_one_incident_that_closes_on_recovery(time4vps_mock, api_client):
    """A repeating Time4VPS failure opens exactly one incident and resolves on recovery."""
    async with httpx.AsyncClient(base_url=api_client.base_url) as client:
        resp = await client.post(
            "/api/api-keys/",
            json={
                "service": "time4vps",
                "type": "credentials",
                "value": {"username": "test", "password": "test"},
                "project_id": None,
            },
        )
        assert resp.status_code == httpx.codes.CREATED, f"Failed to seed API key: {resp.text}"

    failing = time4vps_mock.get("/api/server").respond(
        status_code=401,
        text='{"error":["ipnotallowed","unauthorized"]}',
    )

    time4vps_client = await server_sync.get_time4vps_client()

    for _ in range(3):
        with pytest.raises(Time4VPSAPIError):
            await server_sync._sync_server_list(time4vps_client)

    assert failing.call_count == 3  # noqa: PLR2004

    outages = [
        incident
        for incident in await api_client.list_active_incidents()
        if incident.incident_type is IncidentType.PROVIDER_API_UNAVAILABLE
    ]
    assert len(outages) == 1
    assert outages[0].server_handle is None
    assert "ipnotallowed" in outages[0].details["response_body"]
    assert outages[0].details["status_code"] == httpx.codes.UNAUTHORIZED

    time4vps_mock.get("/api/server").respond(status_code=200, json=[])
    await server_sync._sync_server_list(time4vps_client)

    still_active = [
        incident
        for incident in await api_client.list_active_incidents()
        if incident.incident_type is IncidentType.PROVIDER_API_UNAVAILABLE
    ]
    assert still_active == []
