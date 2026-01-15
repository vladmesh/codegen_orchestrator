from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.api_key import APIKeyDTO
from shared.contracts.dto.server import ServerDTO, ServerStatus
from src.tasks import server_sync


@pytest.fixture
def mock_api_client():
    with patch("src.tasks.server_sync.api_client") as mock:
        yield mock


@pytest.fixture
def mock_time4vps_client():
    mock = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_get_time4vps_client_returns_client(mock_api_client):
    mock_api_client.get_api_key = AsyncMock(
        return_value=APIKeyDTO(
            id=1, service="time4vps", key_enc='{"username": "u", "password": "p"}'
        )
    )

    client = await server_sync.get_time4vps_client()
    assert client is not None
    assert client.username == "u"


@pytest.mark.asyncio
async def test_sync_server_list_discovers_new_managed(mock_api_client, mock_time4vps_client):
    # Setup
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="test.com")
    mock_time4vps_client.get_servers.return_value = [api_server]

    mock_api_client.get_servers = AsyncMock(return_value=[])  # No DB servers

    new_server_dto = ServerDTO(
        id=1,
        handle="vps-1001",
        host="test.com",
        public_ip="1.2.3.4",
        status=ServerStatus.PENDING_SETUP,
        provider_id="1001",
        is_managed=True,
    )
    mock_api_client.create_server = AsyncMock(return_value=new_server_dto)

    # Execution
    d, u, m = await server_sync._sync_server_list(mock_time4vps_client)

    # Verification
    assert d == 1
    mock_api_client.create_server.assert_called_once()
    create_payload = mock_api_client.create_server.call_args[0][0]
    assert create_payload.public_ip == "1.2.3.4"
    assert create_payload.status == ServerStatus.PENDING_SETUP


@pytest.mark.asyncio
async def test_sync_server_details_updates_specs(mock_api_client, mock_time4vps_client):
    # Setup
    server = ServerDTO(
        id=1,
        handle="vps-1",
        host="host",
        public_ip="1.1.1.1",
        status=ServerStatus.ACTIVE,
        provider_id="100",
        is_managed=True,
        labels={"provider_id": "100"},
    )
    mock_api_client.get_servers = AsyncMock(return_value=[server])
    mock_api_client.update_server = AsyncMock()

    details_mock = MagicMock()
    details_mock.model_dump.return_value = {
        "cpu_cores": 4,
        "ram_limit": 8192,
        "disk_limit": 102400,
        "ram_used": 1000,
        "disk_usage": 5000,
        "os": "ubuntu",
        "status": "active",
    }
    mock_time4vps_client.get_server_details.return_value = details_mock

    # Execution
    await server_sync._sync_server_details(mock_time4vps_client)

    # Verification
    mock_api_client.update_server.assert_called_once()
    update_payload = mock_api_client.update_server.call_args[0][1]
    assert update_payload.capacity_cpu == 4  # noqa: PLR2004
    assert update_payload.capacity_ram_mb == 8192  # noqa: PLR2004


@pytest.mark.asyncio
async def test_check_provisioning_triggers_detects_force_rebuild(mock_api_client):
    # Setup
    server = ServerDTO(
        id=1,
        handle="vps-1",
        host="host",
        public_ip="1.1.1.1",
        status=ServerStatus.FORCE_REBUILD,
        provider_id="100",
        is_managed=True,
    )
    mock_api_client.get_servers = AsyncMock(return_value=[server])
    mock_api_client.update_server = AsyncMock()

    with patch(
        "src.tasks.server_sync.publish_provisioner_trigger", new_callable=AsyncMock
    ) as mock_trigger:
        await server_sync._check_provisioning_triggers()

        # Verification
        mock_trigger.assert_called_with("vps-1", is_incident_recovery=False)
        mock_api_client.update_server.assert_called()
        assert mock_api_client.update_server.call_args[0][1].status == ServerStatus.PROVISIONING
