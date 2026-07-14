from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.incident import IncidentDTO, IncidentStatus, IncidentType
from shared.contracts.dto.server import ServerDTO, ServerStatus
from src.tasks import server_sync


@pytest.fixture
def mock_notify_admins():
    with patch("src.tasks.server_sync.notify_admins_best_effort", new_callable=AsyncMock) as mock:
        yield mock


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
        return_value={
            "id": 1,
            "service": "time4vps",
            "value": '{"username": "u", "password": "p"}',
        }
    )

    client = await server_sync.get_time4vps_client()
    assert client is not None
    assert client.username == "u"


@pytest.mark.asyncio
async def test_sync_server_list_discovers_new_managed(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    # Setup
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="test.com")
    mock_time4vps_client.get_servers.return_value = [api_server]

    mock_api_client.get_servers = AsyncMock(return_value=[])  # No DB servers

    new_server_dto = ServerDTO(
        handle="vps-1001",
        host="test.com",
        public_ip="1.2.3.4",
        ssh_user="root",
        status=ServerStatus.PENDING_SETUP,
        provider_id="1001",
        is_managed=True,
        created_at=datetime.now(UTC),
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
        handle="vps-1",
        host="host",
        public_ip="1.1.1.1",
        ssh_user="root",
        status=ServerStatus.ACTIVE,
        provider_id="100",
        is_managed=True,
        labels={"provider_id": "100"},
        created_at=datetime.now(UTC),
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
async def test_check_provisioning_triggers_detects_force_rebuild(
    mock_api_client, mock_notify_admins
):
    # Setup
    server = ServerDTO(
        handle="vps-1",
        host="host",
        public_ip="1.1.1.1",
        ssh_user="root",
        status=ServerStatus.FORCE_REBUILD,
        provider_id="100",
        is_managed=True,
        created_at=datetime.now(UTC),
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


@pytest.mark.asyncio
async def test_force_rebuild_sweep_continues_after_first_notification_failure(mock_api_client):
    first = _ready_server("first").model_copy(update={"status": ServerStatus.FORCE_REBUILD})
    second = _ready_server("second").model_copy(update={"status": ServerStatus.FORCE_REBUILD})
    mock_api_client.get_servers = AsyncMock(return_value=[first, second])
    mock_api_client.update_server = AsyncMock()

    with (
        patch(
            "src.tasks.server_sync.publish_provisioner_trigger", new_callable=AsyncMock
        ) as trigger,
        patch("shared.notifications.notify_admins", new_callable=AsyncMock) as notify,
    ):
        notify.side_effect = RuntimeError("users API unavailable")
        published = await server_sync._check_provisioning_triggers()

    assert published == 2
    assert trigger.await_count == 2
    assert mock_api_client.update_server.await_count == 2


def _incident(incident_id: int, server_handle: str, incident_type: IncidentType) -> IncidentDTO:
    return IncidentDTO(
        id=incident_id,
        server_handle=server_handle,
        incident_type=incident_type,
        status=IncidentStatus.DETECTED,
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _ready_server(handle: str) -> ServerDTO:
    return ServerDTO(
        handle=handle,
        host="host",
        public_ip="1.1.1.1",
        ssh_user="root",
        status=ServerStatus.READY,
        is_managed=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_reconcile_resolves_only_active_provisioning_incidents_for_ready_servers(
    mock_api_client, mock_notify_admins
):
    provisioning = _incident(1, "ready", IncidentType.PROVISIONING_FAILED)
    other_type = _incident(2, "ready", IncidentType.SERVICE_DOWN)
    not_ready = _incident(3, "not-ready", IncidentType.PROVISIONING_FAILED)
    mock_api_client.get_servers = AsyncMock(return_value=[_ready_server("ready")])
    mock_api_client.list_active_incidents = AsyncMock(
        return_value=[provisioning, other_type, not_ready]
    )
    mock_api_client.resolve_incident = AsyncMock()

    resolved = await server_sync._reconcile_provisioning_incidents()

    assert resolved == 1
    mock_api_client.resolve_incident.assert_awaited_once_with(1)
    mock_notify_admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_continues_after_a_journal_failure_without_notification_storm(
    mock_api_client, mock_notify_admins
):
    first = _incident(1, "first", IncidentType.PROVISIONING_FAILED)
    second = _incident(2, "second", IncidentType.PROVISIONING_FAILED)
    mock_api_client.get_servers = AsyncMock(
        return_value=[_ready_server("first"), _ready_server("second")]
    )
    mock_api_client.list_active_incidents = AsyncMock(return_value=[first, second])
    mock_api_client.resolve_incident = AsyncMock(side_effect=[RuntimeError("api down"), None])

    resolved = await server_sync._reconcile_provisioning_incidents()

    assert resolved == 1
    assert mock_api_client.resolve_incident.await_args_list[0].args == (1,)
    assert mock_api_client.resolve_incident.await_args_list[1].args == (2,)
    mock_notify_admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_is_idempotent_after_the_incident_is_resolved(
    mock_api_client, mock_notify_admins
):
    provisioning = _incident(1, "ready", IncidentType.PROVISIONING_FAILED)
    mock_api_client.get_servers = AsyncMock(return_value=[_ready_server("ready")])
    mock_api_client.list_active_incidents = AsyncMock(side_effect=[[provisioning], []])
    mock_api_client.resolve_incident = AsyncMock()

    first = await server_sync._reconcile_provisioning_incidents()
    second = await server_sync._reconcile_provisioning_incidents()

    assert (first, second) == (1, 0)
    mock_api_client.resolve_incident.assert_awaited_once_with(1)
    mock_notify_admins.assert_not_awaited()
