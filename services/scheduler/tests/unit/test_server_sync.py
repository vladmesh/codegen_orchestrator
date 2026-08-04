from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from shared.clients.time4vps import Time4VPSAPIError
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
    mock_api_client, mock_time4vps_client, mock_notify_admins, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    # Setup
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="test.com")
    mock_time4vps_client.get_servers.return_value = [api_server]

    mock_api_client.get_servers = AsyncMock(return_value=[])  # No DB servers
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])

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
async def test_sync_server_list_discovers_unlisted_server_as_reserved(
    mock_api_client, mock_time4vps_client, mock_notify_admins, monkeypatch
):
    monkeypatch.delenv("TIME4VPS_MANAGED_SERVER_IDS", raising=False)
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="personal.example")
    mock_time4vps_client.get_servers.return_value = [api_server]
    mock_api_client.get_servers = AsyncMock(return_value=[])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.create_server = AsyncMock(
        return_value=ServerDTO(
            handle="vps-1001",
            host="personal.example",
            public_ip="1.2.3.4",
            ssh_user="root",
            status=ServerStatus.RESERVED,
            provider_id="1001",
            is_managed=False,
            created_at=datetime.now(UTC),
        )
    )

    await server_sync._sync_server_list(mock_time4vps_client)

    create_payload = mock_api_client.create_server.call_args.args[0]
    assert create_payload.is_managed is False
    assert create_payload.status == ServerStatus.RESERVED
    mock_notify_admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_server_list_demotes_existing_unlisted_pending_server(
    mock_api_client, mock_time4vps_client, mock_notify_admins, monkeypatch
):
    monkeypatch.delenv("TIME4VPS_MANAGED_SERVER_IDS", raising=False)
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="personal.example")
    existing = ServerDTO(
        handle="vps-1001",
        host="personal.example",
        public_ip="1.2.3.4",
        ssh_user="root",
        status=ServerStatus.PENDING_SETUP,
        provider_id="1001",
        is_managed=True,
        labels={"provider_id": "1001"},
        created_at=datetime.now(UTC),
    )
    mock_time4vps_client.get_servers.return_value = [api_server]
    mock_api_client.get_servers = AsyncMock(return_value=[existing])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.update_server = AsyncMock()

    await server_sync._sync_server_list(mock_time4vps_client)

    update = mock_api_client.update_server.call_args.args[1]
    assert update.is_managed is False
    assert update.status == ServerStatus.RESERVED
    mock_notify_admins.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_server_list_promotes_existing_server_without_scheduling_it(
    mock_api_client, mock_time4vps_client, mock_notify_admins, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    api_server = MagicMock(ip="1.2.3.4", id=1001, domain="personal.example")
    existing = ServerDTO(
        handle="vps-1001",
        host="personal.example",
        public_ip="1.2.3.4",
        ssh_user="root",
        status=ServerStatus.RESERVED,
        provider_id="1001",
        is_managed=False,
        labels={"provider_id": "1001"},
        created_at=datetime.now(UTC),
    )
    mock_time4vps_client.get_servers.return_value = [api_server]
    mock_api_client.get_servers = AsyncMock(return_value=[existing])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.update_server = AsyncMock()

    await server_sync._sync_server_list(mock_time4vps_client)

    update = mock_api_client.update_server.call_args.args[1]
    assert update.is_managed is True
    assert "status" not in update.model_fields_set
    mock_notify_admins.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_server_list_tracks_existing_server_by_provider_id_when_ip_changes(
    mock_api_client, mock_time4vps_client, mock_notify_admins, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    api_server = MagicMock(ip="1.2.3.99", id=1001, domain="renamed.example")
    existing = ServerDTO(
        handle="vps-1001",
        host="old.example",
        public_ip="1.2.3.4",
        ssh_user="root",
        status=ServerStatus.READY,
        provider_id="1001",
        is_managed=True,
        labels={"provider_id": "1001"},
        created_at=datetime.now(UTC),
    )
    mock_time4vps_client.get_servers.return_value = [api_server]
    mock_api_client.get_servers = AsyncMock(return_value=[existing])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.update_server = AsyncMock()
    mock_api_client.create_server = AsyncMock()

    discovered, updated, missing = await server_sync._sync_server_list(mock_time4vps_client)

    assert (discovered, updated, missing) == (0, 1, 0)
    update = mock_api_client.update_server.call_args.args[1]
    assert update.public_ip == "1.2.3.99"
    assert update.host == "renamed.example"
    mock_api_client.create_server.assert_not_awaited()


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
    mock_api_client, mock_notify_admins, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "100")
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
        mock_trigger.return_value = True
        await server_sync._check_provisioning_triggers()

        # Verification
        mock_trigger.assert_called_with("vps-1", is_incident_recovery=False)
        mock_api_client.update_server.assert_called()
        assert mock_api_client.update_server.call_args[0][1].status == ServerStatus.PROVISIONING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ServerStatus.FORCE_REBUILD, ServerStatus.PENDING_SETUP, ServerStatus.PROVISIONING],
)
async def test_check_provisioning_triggers_skips_unmanaged_server(
    mock_api_client, mock_notify_admins, status
):
    server = _ready_server("personal").model_copy(
        update={"status": status, "is_managed": False, "provisioning_started_at": None}
    )
    mock_api_client.get_servers = AsyncMock(return_value=[server])
    mock_api_client.update_server = AsyncMock()

    with patch(
        "src.tasks.server_sync.publish_provisioner_trigger", new_callable=AsyncMock
    ) as trigger:
        published = await server_sync._check_provisioning_triggers()

    assert published == 0
    trigger.assert_not_awaited()
    mock_api_client.update_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_provisioning_triggers_skips_stale_managed_row_outside_allowlist(
    mock_api_client, mock_notify_admins, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "200")
    server = _ready_server("stale").model_copy(
        update={"status": ServerStatus.FORCE_REBUILD, "provisioning_started_at": None}
    )
    mock_api_client.get_servers = AsyncMock(return_value=[server])
    mock_api_client.update_server = AsyncMock()

    with patch(
        "src.tasks.server_sync.publish_provisioner_trigger", new_callable=AsyncMock
    ) as trigger:
        published = await server_sync._check_provisioning_triggers()

    assert published == 0
    trigger.assert_not_awaited()
    mock_api_client.update_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_rebuild_sweep_continues_after_first_notification_failure(
    mock_api_client, monkeypatch
):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "100")
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
        trigger.return_value = True
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
        provider_id="100",
        is_managed=True,
        labels={"provider_id": "100"},
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


def _provider_outage_incident(incident_id: int = 7) -> IncidentDTO:
    return IncidentDTO(
        id=incident_id,
        server_handle=None,
        incident_type=IncidentType.PROVIDER_API_UNAVAILABLE,
        status=IncidentStatus.DETECTED,
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_provider_failure_aborts_the_sync_instead_of_reporting_zeros(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.side_effect = Time4VPSAPIError(
        "GET", "https://billing.time4vps.com/api/server", 401, '{"error":["ipnotallowed"]}'
    )
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.create_incident = AsyncMock()

    with pytest.raises(Time4VPSAPIError):
        await server_sync._sync_server_list(mock_time4vps_client)

    mock_api_client.get_servers.assert_not_called()


@pytest.mark.asyncio
async def test_provider_failure_opens_one_incident_carrying_the_response_body(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.side_effect = Time4VPSAPIError(
        "GET", "https://billing.time4vps.com/api/server", 401, '{"error":["ipnotallowed"]}'
    )
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.create_incident = AsyncMock()

    with pytest.raises(Time4VPSAPIError):
        await server_sync._sync_server_list(mock_time4vps_client)

    mock_api_client.create_incident.assert_awaited_once()
    kwargs = mock_api_client.create_incident.await_args.kwargs
    assert kwargs["server_handle"] is None
    assert kwargs["incident_type"] is IncidentType.PROVIDER_API_UNAVAILABLE
    assert kwargs["details"]["status_code"] == 401  # noqa: PLR2004
    assert "ipnotallowed" in kwargs["details"]["response_body"]
    mock_notify_admins.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_provider_failure_does_not_signal_every_cycle(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.side_effect = Time4VPSAPIError(
        "GET", "https://billing.time4vps.com/api/server", 401, '{"error":["ipnotallowed"]}'
    )
    mock_api_client.list_active_incidents = AsyncMock(return_value=[_provider_outage_incident()])
    mock_api_client.create_incident = AsyncMock()

    for _ in range(3):
        with pytest.raises(Time4VPSAPIError):
            await server_sync._sync_server_list(mock_time4vps_client)

    mock_api_client.create_incident.assert_not_awaited()
    mock_notify_admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_provider_resolves_the_outage_incident(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.return_value = []
    mock_api_client.get_servers = AsyncMock(return_value=[])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[_provider_outage_incident(7)])
    mock_api_client.resolve_incident = AsyncMock()

    await server_sync._sync_server_list(mock_time4vps_client)

    mock_api_client.resolve_incident.assert_awaited_once_with(7)
    mock_notify_admins.assert_awaited_once()


@pytest.mark.asyncio
async def test_healthy_cycle_leaves_the_journal_alone(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.return_value = []
    mock_api_client.get_servers = AsyncMock(return_value=[])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.resolve_incident = AsyncMock()
    mock_api_client.create_incident = AsyncMock()

    await server_sync._sync_server_list(mock_time4vps_client)

    mock_api_client.resolve_incident.assert_not_awaited()
    mock_api_client.create_incident.assert_not_awaited()


async def _run_one_worker_cycle() -> list[dict]:
    """Run a single sync_servers_worker iteration and return the emitted log entries."""
    with (
        patch("src.tasks.server_sync.asyncio.sleep", side_effect=_StopWorker),
        patch("src.tasks.server_sync._sync_interval", return_value=1),
        patch("src.tasks.server_sync._details_sync_interval", return_value=10_000),
        capture_logs() as logs,
        pytest.raises(_StopWorker),
    ):
        await server_sync.sync_servers_worker()
    return logs


class _StopWorker(Exception):
    """Breaks the worker's infinite loop after one cycle."""


@pytest.mark.asyncio
async def test_failed_cycle_is_not_logged_as_a_completed_sync(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.side_effect = Time4VPSAPIError(
        "GET", "https://billing.time4vps.com/api/server", 401, '{"error":["ipnotallowed"]}'
    )
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])
    mock_api_client.create_incident = AsyncMock()

    with patch(
        "src.tasks.server_sync.get_time4vps_client",
        new=AsyncMock(return_value=mock_time4vps_client),
    ):
        logs = await _run_one_worker_cycle()

    events = [entry["event"] for entry in logs]
    assert "server_sync_complete" not in events
    incomplete = [entry for entry in logs if entry["event"] == "server_sync_incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0]["log_level"] == "error"
    assert "ipnotallowed" in incomplete[0]["reason"]


@pytest.mark.asyncio
async def test_successful_empty_cycle_still_reports_completion(
    mock_api_client, mock_time4vps_client, mock_notify_admins
):
    mock_time4vps_client.get_servers.return_value = []
    mock_api_client.get_servers = AsyncMock(return_value=[])
    mock_api_client.list_active_incidents = AsyncMock(return_value=[])

    with patch(
        "src.tasks.server_sync.get_time4vps_client",
        new=AsyncMock(return_value=mock_time4vps_client),
    ):
        logs = await _run_one_worker_cycle()

    events = [entry["event"] for entry in logs]
    assert "server_sync_complete" in events
    assert "server_sync_incomplete" not in events
