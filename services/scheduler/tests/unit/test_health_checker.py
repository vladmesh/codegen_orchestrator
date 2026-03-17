"""Unit tests for health_checker worker."""

from __future__ import annotations

import os

# Must set before importing health_checker (module-level config)
os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _http_response(status_code: int = 200, text: str = "") -> httpx.Response:
    """Create an httpx.Response with a fake request attached."""
    request = httpx.Request("GET", "http://test/metrics")
    return httpx.Response(status_code, text=text, request=request)


# ── Fixtures ──


@pytest.fixture
def mock_api_client():
    """Mock the SchedulerAPIClient singleton."""
    client = AsyncMock()
    client.get_servers = AsyncMock(return_value=[])
    client.update_server = AsyncMock()
    client.create_metrics_history = AsyncMock()
    client.create_incident = AsyncMock(return_value={"id": 1})
    client.get_active_incidents = AsyncMock(return_value=[])
    client.resolve_incident = AsyncMock()
    return client


def _make_server(
    handle: str = "vps-123",
    public_ip: str = "10.0.0.1",
    status: str = "active",
    is_managed: bool = True,
) -> MagicMock:
    """Create a mock ServerDTO."""
    srv = MagicMock()
    srv.handle = handle
    srv.public_ip = public_ip
    srv.status = status
    srv.is_managed = is_managed
    srv.capacity_ram_mb = 4096
    srv.capacity_disk_mb = 40960
    return srv


NODE_EXPORTER_TEXT = """\
# HELP node_cpu_seconds_total CPU seconds total
node_cpu_seconds_total{cpu="0",mode="idle"} 9000
node_cpu_seconds_total{cpu="0",mode="user"} 500
node_cpu_seconds_total{cpu="0",mode="system"} 300
node_cpu_seconds_total{cpu="0",mode="iowait"} 200
node_memory_MemTotal_bytes 4294967296
node_memory_MemAvailable_bytes 2147483648
node_filesystem_size_bytes{mountpoint="/"} 42949672960
node_filesystem_avail_bytes{mountpoint="/"} 21474836480
node_load1 0.5
node_load5 0.3
node_load15 0.1
node_boot_time_seconds 1710000000
node_network_receive_errs_total{device="eth0"} 5
node_network_transmit_errs_total{device="eth0"} 2
"""

CADVISOR_TEXT = """\
# HELP container_cpu_usage_seconds_total CPU usage
container_cpu_usage_seconds_total{name="web-app",id="/docker/abc123"} 100.5
container_memory_usage_bytes{name="web-app",id="/docker/abc123"} 536870912
container_spec_memory_limit_bytes{name="web-app",id="/docker/abc123"} 1073741824
container_network_receive_bytes_total{name="web-app",id="/docker/abc123"} 1000000
container_network_transmit_bytes_total{name="web-app",id="/docker/abc123"} 500000
container_cpu_usage_seconds_total{name="redis",id="/docker/def456"} 50.2
container_memory_usage_bytes{name="redis",id="/docker/def456"} 268435456
"""


# ── Test: _check_server ──


class TestCheckServer:
    """Tests for the per-server health check logic."""

    @pytest.mark.asyncio
    async def test_successful_check_updates_server_and_history(self, mock_api_client):
        """Successful HTTP fetch → update server metrics + append history."""
        server = _make_server()

        node_resp = _http_response(200, NODE_EXPORTER_TEXT)
        cadvisor_resp = _http_response(200, CADVISOR_TEXT)

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=[node_resp, cadvisor_resp])

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        # Server should be updated with parsed metrics
        mock_api_client.update_server.assert_called_once()
        call_args = mock_api_client.update_server.call_args
        assert call_args[0][0] == "vps-123"
        update = call_args[0][1]
        assert update.cpu_usage_pct is not None
        assert update.load_avg_1m == 0.5
        assert update.container_count_running == 2
        assert update.last_health_check is not None

        # History should be appended
        mock_api_client.create_metrics_history.assert_called_once()
        hist_args = mock_api_client.create_metrics_history.call_args
        assert hist_args[0][0] == "vps-123"
        metrics_dict = hist_args[0][1]
        assert "cpu_usage_pct" in metrics_dict
        assert "containers" in metrics_dict

    @pytest.mark.asyncio
    async def test_node_exporter_timeout_creates_incident(self, mock_api_client):
        """HTTP timeout on node_exporter → SERVER_UNREACHABLE incident."""
        server = _make_server()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
            patch("src.tasks.health_checker.notify_admins", new_callable=AsyncMock) as mock_notify,
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        # Incident should be created
        mock_api_client.create_incident.assert_called_once()
        call_args = mock_api_client.create_incident.call_args
        assert call_args[1]["incident_type"] == "server_unreachable"
        assert call_args[1]["server_handle"] == "vps-123"

        # Admin should be notified
        mock_notify.assert_called_once()
        notify_args = mock_notify.call_args
        assert "vps-123" in notify_args[0][0]
        assert notify_args[1]["level"] == "critical"

    @pytest.mark.asyncio
    async def test_duplicate_incident_not_created(self, mock_api_client):
        """If active incident exists for same type, don't create another."""
        server = _make_server()
        mock_api_client.get_active_incidents.return_value = [{"id": 1}]

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
            patch("src.tasks.health_checker.notify_admins", new_callable=AsyncMock) as mock_notify,
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        # No new incident created
        mock_api_client.create_incident.assert_not_called()
        # No notification sent
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_resolves_incident(self, mock_api_client):
        """Successful check when SERVER_UNREACHABLE incident exists → auto-resolve."""
        server = _make_server()
        mock_api_client.get_active_incidents.return_value = [{"id": 5}]

        node_resp = _http_response(200, NODE_EXPORTER_TEXT)
        cadvisor_resp = _http_response(200, CADVISOR_TEXT)

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=[node_resp, cadvisor_resp])

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
            patch("src.tasks.health_checker.notify_admins", new_callable=AsyncMock) as mock_notify,
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        # Incident should be resolved
        mock_api_client.resolve_incident.assert_called_once_with(5)
        # Recovery notification
        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["level"] == "success"


class TestResourceExhaustion:
    """Tests for RAM/disk threshold alerting."""

    @pytest.mark.asyncio
    async def test_high_ram_creates_resource_exhausted_incident(self, mock_api_client):
        """RAM > 90% → RESOURCE_EXHAUSTED incident."""
        server = _make_server()

        # RAM: 3.8GB used of 4GB = 95%
        high_ram_text = """\
node_memory_MemTotal_bytes 4294967296
node_memory_MemAvailable_bytes 214748365
node_filesystem_size_bytes{mountpoint="/"} 42949672960
node_filesystem_avail_bytes{mountpoint="/"} 21474836480
node_load1 0.5
"""
        node_resp = _http_response(200, high_ram_text)
        cadvisor_resp = _http_response(200, "")

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=[node_resp, cadvisor_resp])

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
            patch("src.tasks.health_checker.notify_admins", new_callable=AsyncMock) as mock_notify,
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        # RESOURCE_EXHAUSTED incident should be created
        mock_api_client.create_incident.assert_called_once()
        call_kwargs = mock_api_client.create_incident.call_args[1]
        assert call_kwargs["incident_type"] == "resource_exhausted"
        assert "ram" in call_kwargs["details"]["resource"]

        # Notification should be sent
        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_high_disk_creates_resource_exhausted_incident(self, mock_api_client):
        """Disk > 90% → RESOURCE_EXHAUSTED incident."""
        server = _make_server()

        # Disk: ~95% used
        high_disk_text = """\
node_memory_MemTotal_bytes 4294967296
node_memory_MemAvailable_bytes 2147483648
node_filesystem_size_bytes{mountpoint="/"} 42949672960
node_filesystem_avail_bytes{mountpoint="/"} 2147483648
node_load1 0.5
"""
        node_resp = _http_response(200, high_disk_text)
        cadvisor_resp = _http_response(200, "")

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=[node_resp, cadvisor_resp])

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch("src.tasks.health_checker._get_http_client", return_value=mock_http),
            patch("src.tasks.health_checker.notify_admins", new_callable=AsyncMock),
        ):
            from src.tasks.health_checker import _check_server

            await _check_server(server)

        mock_api_client.create_incident.assert_called_once()
        call_kwargs = mock_api_client.create_incident.call_args[1]
        assert call_kwargs["incident_type"] == "resource_exhausted"
        assert "disk" in call_kwargs["details"]["resource"]


class TestFilterServers:
    """Tests for server filtering logic."""

    @pytest.mark.asyncio
    async def test_only_managed_active_servers_checked(self, mock_api_client):
        """Only managed servers with active/in_use status are health-checked."""
        servers = [
            _make_server("s1", "10.0.0.1", "active", is_managed=True),
            _make_server("s2", "10.0.0.2", "provisioning", is_managed=True),
            _make_server("s3", "10.0.0.3", "active", is_managed=False),
            _make_server("s4", "10.0.0.4", "in_use", is_managed=True),
            _make_server("s5", "10.0.0.5", "ready", is_managed=True),
        ]

        with patch("src.tasks.health_checker.api_client", mock_api_client):
            from src.tasks.health_checker import _get_checkable_servers

            result = _get_checkable_servers(servers)

        handles = [s.handle for s in result]
        assert handles == ["s1", "s4", "s5"]


class TestCleanupHistory:
    """Tests for daily metrics history cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_api(self, mock_api_client):
        """_cleanup_old_history calls delete_old_metrics_history."""
        mock_api_client.delete_old_metrics_history = AsyncMock(return_value={"deleted": 10})
        mock_api_client.delete_old_app_health_history = AsyncMock(return_value={"deleted": 0})

        with patch("src.tasks.health_checker.api_client", mock_api_client):
            from src.tasks.health_checker import _cleanup_old_history

            deleted = await _cleanup_old_history()

        mock_api_client.delete_old_metrics_history.assert_called_once_with(168)
        assert deleted == 10


class TestAppHealthIntegration:
    """Tests for app health prober integration into health_check_worker."""

    @pytest.mark.asyncio
    async def test_health_check_worker_calls_app_probe_cycle(self, mock_api_client):
        """health_check_worker calls app_health_probe_cycle after server checks."""
        mock_api_client.get_servers.return_value = []

        class _BreakLoop(Exception):
            pass

        with (
            patch("src.tasks.health_checker.api_client", mock_api_client),
            patch(
                "src.tasks.health_checker.app_health_probe_cycle", new_callable=AsyncMock
            ) as mock_app_probe,
            patch("src.tasks.health_checker.asyncio.sleep", side_effect=_BreakLoop),
        ):
            from src.tasks.health_checker import health_check_worker

            with pytest.raises(_BreakLoop):
                await health_check_worker()

        mock_app_probe.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_includes_app_health_history(self, mock_api_client):
        """Daily cleanup also deletes old app health history."""
        mock_api_client.delete_old_metrics_history = AsyncMock(return_value={"deleted": 5})
        mock_api_client.delete_old_app_health_history = AsyncMock(return_value={"deleted": 3})

        with patch("src.tasks.health_checker.api_client", mock_api_client):
            from src.tasks.health_checker import _cleanup_old_history

            await _cleanup_old_history()

        mock_api_client.delete_old_metrics_history.assert_called_once()
        mock_api_client.delete_old_app_health_history.assert_called_once_with(168)
