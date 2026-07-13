"""Unit tests for application health prober."""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.incident import IncidentDTO


def _make_app(
    app_id: int = 1,
    service_name: str = "web-app",
    server_handle: str = "vps-123",
    status: str = "running",
    ports: list[dict] | None = None,
) -> ApplicationDTO:
    """Create a mock ApplicationDTO as returned by the API."""
    return ApplicationDTO(
        id=app_id,
        repo_id="repo-1",
        service_name=service_name,
        server_handle=server_handle,
        status=status,
        response_time_ms=None,
        ssl_expires_at=None,
        uptime_pct_24h=None,
        last_health_check=None,
        ports=ports if ports is not None else [{"port": 8080, "service_name": "web-app"}],
        created_at=datetime.now(UTC),
    )


def _make_incident(
    incident_id: int = 1,
    server_handle: str = "vps-123",
    incident_type: str = "service_down",
    status: str = "detected",
) -> IncidentDTO:
    """Create a mock IncidentDTO as returned by the API."""
    return IncidentDTO(
        id=incident_id,
        server_handle=server_handle,
        incident_type=incident_type,
        status=status,
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_api_client():
    """Mock SchedulerAPIClient."""
    client = AsyncMock()
    client.get_applications = AsyncMock(return_value=[])
    client.get_servers = AsyncMock(return_value=[])
    client.update_application = AsyncMock(return_value={})
    client.create_app_health_history = AsyncMock(return_value={})
    client.create_incident = AsyncMock(return_value=_make_incident())
    client.get_active_incidents = AsyncMock(return_value=[])
    client.resolve_incident = AsyncMock()
    client.delete_old_app_health_history = AsyncMock(return_value={"deleted": 0})
    return client


class TestCheckApplication:
    """Tests for check_application function."""

    @pytest.mark.asyncio
    async def test_healthy_app_updates_status_and_response_time(self, mock_api_client):
        """Healthy HTTP response → update Application with running status + response_time."""
        app = _make_app()
        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 45}

        with (
            patch(
                "src.tasks.app_health_prober.check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch(
                "src.tasks.app_health_prober.check_ssl_expiry", new_callable=AsyncMock
            ) as mock_ssl,
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            from src.tasks.app_health_prober import check_application

            fail_count = await check_application(
                app=app,
                server_ip="10.0.0.1",
                consecutive_failures=0,
                api_client=mock_api_client,
            )

        assert fail_count == 0
        mock_api_client.update_application.assert_called_once()
        update_kwargs = mock_api_client.update_application.call_args[0]
        fields = update_kwargs[1]
        assert fields["response_time_ms"] == 45
        assert fields["status"] == "running"
        assert "last_health_check" in fields

    @pytest.mark.asyncio
    async def test_unhealthy_app_increments_fail_counter(self, mock_api_client):
        """Unhealthy HTTP response → increment failure counter, update status to down."""
        app = _make_app()
        health_result = {"healthy": False, "error": "timeout", "response_time_ms": 5000}

        with (
            patch(
                "src.tasks.app_health_prober.check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch(
                "src.tasks.app_health_prober.check_ssl_expiry", new_callable=AsyncMock
            ) as mock_ssl,
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            from src.tasks.app_health_prober import check_application

            fail_count = await check_application(
                app=app,
                server_ip="10.0.0.1",
                consecutive_failures=0,
                api_client=mock_api_client,
            )

        assert fail_count == 1

    @pytest.mark.asyncio
    async def test_three_consecutive_fails_creates_service_down_incident(self, mock_api_client):
        """3 consecutive failures → create SERVICE_DOWN incident."""
        app = _make_app()
        health_result = {"healthy": False, "error": "timeout", "response_time_ms": 5000}

        with (
            patch(
                "src.tasks.app_health_prober.check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch(
                "src.tasks.app_health_prober.check_ssl_expiry", new_callable=AsyncMock
            ) as mock_ssl,
            patch("src.tasks.app_health_prober.notify_admins_best_effort", new_callable=AsyncMock),
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            from src.tasks.app_health_prober import check_application

            fail_count = await check_application(
                app=app,
                server_ip="10.0.0.1",
                consecutive_failures=2,  # This will be the 3rd failure
                api_client=mock_api_client,
            )

        assert fail_count == 3
        mock_api_client.create_incident.assert_called_once()
        call_kwargs = mock_api_client.create_incident.call_args[1]
        assert call_kwargs["incident_type"] == "service_down"

    @pytest.mark.asyncio
    async def test_ssl_expiry_near_creates_ssl_expiring_incident(self, mock_api_client):
        """SSL cert expiring within 7 days → create SSL_EXPIRING incident."""
        app = _make_app()
        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 45}
        expiry_soon = datetime.now(UTC) + timedelta(days=5)

        with (
            patch(
                "src.tasks.app_health_prober.check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch(
                "src.tasks.app_health_prober.check_ssl_expiry", new_callable=AsyncMock
            ) as mock_ssl,
            patch("src.tasks.app_health_prober.notify_admins_best_effort", new_callable=AsyncMock),
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = expiry_soon

            from src.tasks.app_health_prober import check_application

            await check_application(
                app=app,
                server_ip="10.0.0.1",
                consecutive_failures=0,
                api_client=mock_api_client,
            )

        mock_api_client.create_incident.assert_called_once()
        call_kwargs = mock_api_client.create_incident.call_args[1]
        assert call_kwargs["incident_type"] == "ssl_expiring"

    @pytest.mark.asyncio
    async def test_recovery_resets_fail_count_and_resolves_incident(self, mock_api_client):
        """Recovery after failures → reset fail count, auto-resolve incidents."""
        app = _make_app()
        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 45}
        mock_api_client.get_active_incidents.return_value = [_make_incident(incident_id=42)]

        with (
            patch(
                "src.tasks.app_health_prober.check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch(
                "src.tasks.app_health_prober.check_ssl_expiry", new_callable=AsyncMock
            ) as mock_ssl,
            patch("src.tasks.app_health_prober.notify_admins_best_effort", new_callable=AsyncMock),
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            from src.tasks.app_health_prober import check_application

            fail_count = await check_application(
                app=app,
                server_ip="10.0.0.1",
                consecutive_failures=5,
                api_client=mock_api_client,
            )

        assert fail_count == 0
        mock_api_client.resolve_incident.assert_called_once_with(42)


class TestAppHealthProbeCycle:
    """Tests for the full probe cycle."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        """Clear module-level state between tests."""
        import src.tasks.app_health_prober as mod

        mod._consecutive_failures.clear()

    @pytest.mark.asyncio
    async def test_skips_not_deployed_apps(self, mock_api_client):
        """Apps with status not_deployed should not be probed."""
        from src.tasks import app_health_prober

        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, status="not_deployed"),
        ]

        with (
            patch.object(
                app_health_prober, "check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock),
        ):
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_probes_running_apps(self, mock_api_client):
        """Running apps with ports should be probed."""
        from unittest.mock import MagicMock

        from src.tasks import app_health_prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api_client.get_servers.return_value = [server]
        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, status="running", server_handle="vps-123"),
        ]

        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 30}

        with (
            patch.object(
                app_health_prober, "check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as mock_ssl,
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            await app_health_prober.app_health_probe_cycle(mock_api_client)

        mock_http.assert_called_once()
        mock_api_client.update_application.assert_called_once()
        mock_api_client.create_app_health_history.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_app_without_ports(self, mock_api_client):
        """Apps with no port allocations should be skipped."""
        from unittest.mock import MagicMock

        from src.tasks import app_health_prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api_client.get_servers.return_value = [server]
        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, status="running", server_handle="vps-123", ports=[]),
        ]

        with (
            patch.object(
                app_health_prober, "check_http_health", new_callable=AsyncMock
            ) as mock_http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock),
        ):
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        mock_http.assert_not_called()
