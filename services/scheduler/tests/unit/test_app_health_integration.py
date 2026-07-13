"""Integration test for full application health probe flow.

Tests the complete flow: app_health_probe_cycle with mocked HTTP
verifying Application fields updated, health history created,
and incident handling works end-to-end.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
        ports=ports if ports is not None else [{"port": 8080, "service_name": service_name}],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_api():
    """Full mock API client for integration test."""
    client = AsyncMock()
    client.get_applications = AsyncMock(return_value=[])
    client.get_servers = AsyncMock(return_value=[])
    client.update_application = AsyncMock(return_value={})
    client.create_app_health_history = AsyncMock(return_value={})
    client.create_incident = AsyncMock(return_value={"id": 1})
    client.get_active_incidents = AsyncMock(return_value=[])
    client.resolve_incident = AsyncMock()
    client.delete_old_app_health_history = AsyncMock(return_value={"deleted": 0})
    return client


class TestFullProbeFlow:
    """End-to-end flow: create app → probe → verify updates + history + incidents."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        """Clear module-level state between tests."""
        import src.tasks.app_health_prober as mod

        mod._consecutive_failures.clear()

    @pytest.mark.asyncio
    async def test_healthy_app_full_flow(self, mock_api):
        """Healthy app: status=running, response_time set, history created."""
        import src.tasks.app_health_prober as prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api.get_servers.return_value = [server]
        mock_api.get_applications.return_value = [
            _make_app(app_id=1, status="running"),
        ]

        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 42}

        with (
            patch.object(prober, "check_http_health", new_callable=AsyncMock) as mock_http,
            patch.object(prober, "check_ssl_expiry", new_callable=AsyncMock) as mock_ssl,
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = datetime.now(UTC) + timedelta(days=90)

            await prober.app_health_probe_cycle(mock_api)

        # Application updated with running status + response time
        mock_api.update_application.assert_called()
        update_call = mock_api.update_application.call_args_list[0]
        fields = update_call[0][1]
        assert fields["status"] == "running"
        assert fields["response_time_ms"] == 42
        assert "last_health_check" in fields
        assert "ssl_expires_at" in fields

        # Health history created
        mock_api.create_app_health_history.assert_called_once()
        hist_call = mock_api.create_app_health_history.call_args[0]
        assert hist_call[0] == 1  # app_id
        assert hist_call[1]["healthy"] is True

        # No incidents created (healthy + SSL far from expiry)
        mock_api.create_incident.assert_not_called()

    @pytest.mark.asyncio
    async def test_three_failures_then_recovery_flow(self, mock_api):
        """3 consecutive failures → SERVICE_DOWN → recovery → auto-resolve."""
        import src.tasks.app_health_prober as prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api.get_servers.return_value = [server]
        mock_api.get_applications.return_value = [
            _make_app(app_id=1, status="running"),
        ]

        fail_result = {"healthy": False, "error": "timeout", "response_time_ms": 5000}
        ok_result = {"healthy": True, "status_code": 200, "response_time_ms": 50}

        with (
            patch.object(prober, "check_http_health", new_callable=AsyncMock) as mock_http,
            patch.object(prober, "check_ssl_expiry", new_callable=AsyncMock) as mock_ssl,
            patch.object(prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            mock_ssl.return_value = None

            # 3 consecutive failures
            for _ in range(3):
                mock_http.return_value = fail_result
                mock_api.get_active_incidents.return_value = []
                await prober.app_health_probe_cycle(mock_api)

            # SERVICE_DOWN incident should have been created
            assert mock_api.create_incident.call_count == 1
            incident_call = mock_api.create_incident.call_args[1]
            assert incident_call["incident_type"] == "service_down"

            # Now recover
            mock_http.return_value = ok_result
            mock_api.get_active_incidents.return_value = [
                IncidentDTO(
                    id=42,
                    server_handle="vps-123",
                    incident_type="service_down",
                    status="detected",
                    detected_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            ]
            await prober.app_health_probe_cycle(mock_api)

        # Incident should be resolved
        mock_api.resolve_incident.assert_called_with(42)

    @pytest.mark.asyncio
    async def test_ssl_expiry_creates_incident(self, mock_api):
        """SSL cert expiring in 5 days → SSL_EXPIRING incident."""
        import src.tasks.app_health_prober as prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api.get_servers.return_value = [server]
        mock_api.get_applications.return_value = [
            _make_app(app_id=1, status="running"),
        ]

        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 30}
        expiry_soon = datetime.now(UTC) + timedelta(days=5)

        with (
            patch.object(prober, "check_http_health", new_callable=AsyncMock) as mock_http,
            patch.object(prober, "check_ssl_expiry", new_callable=AsyncMock) as mock_ssl,
            patch.object(prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = expiry_soon

            await prober.app_health_probe_cycle(mock_api)

        # SSL_EXPIRING incident created
        mock_api.create_incident.assert_called_once()
        call_kwargs = mock_api.create_incident.call_args[1]
        assert call_kwargs["incident_type"] == "ssl_expiring"
        assert call_kwargs["details"]["days_until_expiry"] in (4, 5)  # depends on time of day

    @pytest.mark.asyncio
    async def test_multiple_apps_probed_independently(self, mock_api):
        """Multiple apps on same server probed independently."""
        import src.tasks.app_health_prober as prober

        server = MagicMock()
        server.handle = "vps-123"
        server.public_ip = "10.0.0.1"
        mock_api.get_servers.return_value = [server]
        mock_api.get_applications.return_value = [
            _make_app(app_id=1, service_name="api", ports=[{"port": 8080, "service_name": "api"}]),
            _make_app(app_id=2, service_name="web", ports=[{"port": 3000, "service_name": "web"}]),
        ]

        health_result = {"healthy": True, "status_code": 200, "response_time_ms": 30}

        with (
            patch.object(prober, "check_http_health", new_callable=AsyncMock) as mock_http,
            patch.object(prober, "check_ssl_expiry", new_callable=AsyncMock) as mock_ssl,
        ):
            mock_http.return_value = health_result
            mock_ssl.return_value = None

            await prober.app_health_probe_cycle(mock_api)

        # Both apps should be probed (2 HTTP checks)
        assert mock_http.call_count == 2
        urls = [call[0][0] for call in mock_http.call_args_list]
        assert "http://10.0.0.1:8080/health" in urls
        assert "http://10.0.0.1:3000/health" in urls

        # Both apps updated and history created
        assert mock_api.update_application.call_count == 2
        assert mock_api.create_app_health_history.call_count == 2
