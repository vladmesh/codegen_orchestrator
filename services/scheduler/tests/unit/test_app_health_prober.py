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
    monitoring_enabled: bool = True,
    monitoring_changed_at: datetime | None = None,
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
        monitoring_enabled=monitoring_enabled,
        monitoring_changed_at=monitoring_changed_at,
        created_at=datetime.now(UTC),
    )


def _make_incident(
    incident_id: int = 1,
    server_handle: str = "vps-123",
    incident_type: str = "service_down",
    status: str = "detected",
    application_id: int | None = 1,
) -> IncidentDTO:
    """Create a mock IncidentDTO as returned by the API."""
    return IncidentDTO(
        id=incident_id,
        server_handle=server_handle,
        incident_type=incident_type,
        status=status,
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        details={} if application_id is None else {"application_id": application_id},
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
    client.list_active_incidents = AsyncMock(return_value=[])
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
        mock_api_client.list_active_incidents.return_value = [_make_incident(incident_id=42)]

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
        mock_api_client.resolve_incident.assert_called_once_with(
            42, monitoring_generation="initial"
        )


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


_FAIL = {"healthy": False, "error": "timeout", "response_time_ms": 5000}
_OK = {"healthy": True, "status_code": 200, "response_time_ms": 30}


def _server(handle: str = "vps-123"):
    from unittest.mock import MagicMock

    server = MagicMock()
    server.handle = handle
    server.public_ip = "10.0.0.1"
    return server


class TestMonitoringSwitch:
    """An application with monitoring disabled is neither probed nor alerted about."""

    @pytest.fixture(autouse=True)
    def _clear_state(self):
        import src.tasks.app_health_prober as mod

        mod._consecutive_failures.clear()

    @pytest.mark.asyncio
    async def test_disabled_app_is_not_probed_while_others_are(self, mock_api_client):
        from src.tasks import app_health_prober

        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, monitoring_enabled=False),
            _make_app(app_id=2, ports=[{"port": 9090, "service_name": "other"}]),
        ]
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
        ):
            http.return_value = _OK
            ssl.return_value = None
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        assert http.call_count == 1
        assert ":9090" in http.call_args.args[0]
        assert [c.args[0] for c in mock_api_client.update_application.call_args_list] == [2]
        assert [c.args[0] for c in mock_api_client.create_app_health_history.call_args_list] == [2]

    @pytest.mark.asyncio
    async def test_disabling_forgets_failure_count(self, mock_api_client):
        from src.tasks import app_health_prober

        app_health_prober._consecutive_failures[1] = 2
        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, monitoring_enabled=False)
        ]
        with patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http:
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        http.assert_not_called()
        assert 1 not in app_health_prober._consecutive_failures

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("health", "ssl_expiry", "failures", "open_incident"),
        [
            (_FAIL, None, 2, False),  # SERVICE_DOWN creation
            (_OK, "soon", 0, False),  # SSL_EXPIRING creation
            (_OK, None, 3, True),  # SERVICE_DOWN recovery
        ],
        ids=["service_down", "ssl_expiring", "recovery"],
    )
    async def test_switch_flipped_during_probe_suppresses_alert(
        self, mock_api_client, health, ssl_expiry, failures, open_incident
    ):
        """An in-flight probe whose incident write the API refuses (409) sends no alert.

        The API checks the switch under the application's row lock, the lock the
        switch itself takes, so the refusal is the synchronization point.
        """
        import httpx

        from src.tasks import app_health_prober

        refused = httpx.HTTPStatusError(
            "muted",
            request=httpx.Request("POST", "http://api/incidents/"),
            response=httpx.Response(409),
        )
        mock_api_client.create_incident = AsyncMock(side_effect=refused)
        mock_api_client.resolve_incident = AsyncMock(side_effect=refused)
        if open_incident:
            mock_api_client.list_active_incidents.return_value = [_make_incident(incident_id=9)]
        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [_make_app(app_id=1)]
        app_health_prober._consecutive_failures[1] = failures
        expiry = datetime.now(UTC) + timedelta(days=2) if ssl_expiry else None
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(
                app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock
            ) as notify,
        ):
            http.return_value = health
            ssl.return_value = expiry
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        notify.assert_not_called()
        for call in [
            *mock_api_client.create_incident.call_args_list,
            *mock_api_client.resolve_incident.call_args_list,
        ]:
            assert call.kwargs["monitoring_generation"] == "initial"
        assert (
            mock_api_client.create_incident.call_count + mock_api_client.resolve_incident.call_count
            == 1
        )
        # The refused probe's count does not survive into a later re-enable.
        assert 1 not in app_health_prober._consecutive_failures

    @pytest.mark.asyncio
    async def test_off_on_between_cycles_restarts_failure_count(self, mock_api_client):
        """A switch never observed as off still invalidates the count taken before it."""
        from src.tasks import app_health_prober

        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [_make_app(app_id=1)]
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            http.return_value = _FAIL
            ssl.return_value = None
            for _ in range(2):
                await app_health_prober.app_health_probe_cycle(mock_api_client)
            assert app_health_prober._consecutive_failures[1] == 2

            mock_api_client.get_applications.return_value = [
                _make_app(app_id=1, monitoring_changed_at=datetime.now(UTC))
            ]
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        assert app_health_prober._consecutive_failures[1] == 1
        mock_api_client.create_incident.assert_not_called()

    @pytest.mark.asyncio
    async def test_reenabled_healthy_app_resolves_its_open_incident(self, mock_api_client):
        """No failure history (re-enabled) + healthy probe closes its own open incident."""
        from src.tasks import app_health_prober

        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [_make_app(app_id=1)]
        mock_api_client.list_active_incidents.return_value = [
            _make_incident(incident_id=39, application_id=1)
        ]
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            http.return_value = _OK
            ssl.return_value = None
            await app_health_prober.app_health_probe_cycle(mock_api_client)

        mock_api_client.resolve_incident.assert_called_once_with(
            39, monitoring_generation="initial"
        )


class TestIncidentsAreScopedToApplication:
    """Issue #632: SERVICE_DOWN identity is the application, not the server."""

    @pytest.mark.asyncio
    async def test_healthy_app_does_not_close_sibling_incident(self, mock_api_client):
        from src.tasks import app_health_prober

        mock_api_client.list_active_incidents.return_value = [
            _make_incident(incident_id=7, application_id=1)
        ]
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            http.return_value = _OK
            ssl.return_value = None
            await app_health_prober.check_application(
                app=_make_app(app_id=2),
                server_ip="10.0.0.1",
                consecutive_failures=3,
                api_client=mock_api_client,
            )

        mock_api_client.resolve_incident.assert_not_called()

    @pytest.mark.asyncio
    async def test_sibling_incident_does_not_suppress_own_alert(self, mock_api_client):
        from src.tasks import app_health_prober

        mock_api_client.list_active_incidents.return_value = [
            _make_incident(incident_id=7, application_id=1)
        ]
        with (
            patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            http.return_value = _FAIL
            ssl.return_value = None
            await app_health_prober.check_application(
                app=_make_app(app_id=2),
                server_ip="10.0.0.1",
                consecutive_failures=2,
                api_client=mock_api_client,
            )

        mock_api_client.create_incident.assert_called_once()
        assert mock_api_client.create_incident.call_args.kwargs["details"]["application_id"] == 2

    @pytest.mark.asyncio
    async def test_two_apps_one_server_down_and_healthy_cycle(self, mock_api_client):
        """A stays down while B blips and recovers: A keeps exactly one open incident."""
        from src.tasks import app_health_prober

        app_health_prober._consecutive_failures.clear()
        incidents: list[IncidentDTO] = []

        async def create_incident(**kwargs):
            incident = _make_incident(
                incident_id=100 + len(incidents),
                application_id=kwargs["details"]["application_id"],
            )
            incidents.append(incident)
            return incident

        async def resolve(incident_id):
            incidents[:] = [i for i in incidents if i.id != incident_id]

        mock_api_client.create_incident = AsyncMock(side_effect=create_incident)
        mock_api_client.resolve_incident = AsyncMock(side_effect=resolve)
        mock_api_client.list_active_incidents = AsyncMock(side_effect=lambda *a: list(incidents))
        mock_api_client.get_servers.return_value = [_server()]
        mock_api_client.get_applications.return_value = [
            _make_app(app_id=1, ports=[{"port": 8001, "service_name": "a"}]),
            _make_app(app_id=2, ports=[{"port": 8002, "service_name": "b"}]),
        ]

        cycle = 0

        async def health(url):
            if ":8001" in url:
                return _FAIL
            # B fails once after A's incident exists, then recovers.
            return _FAIL if cycle == 4 else _OK

        with (
            patch.object(app_health_prober, "check_http_health", side_effect=health),
            patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
            patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
        ):
            ssl.return_value = None
            for cycle in range(1, 8):  # noqa: B007 - read by health()
                await app_health_prober.app_health_probe_cycle(mock_api_client)

        assert mock_api_client.create_incident.call_count == 1
        mock_api_client.resolve_incident.assert_not_called()
        assert [i.details["application_id"] for i in incidents] == [1]


def test_monitoring_generation_names_the_switch_a_probe_started_from():
    from src.tasks.app_health_prober import monitoring_generation

    assert monitoring_generation(_make_app()) == "initial"
    at = datetime(2026, 10, 1, 18, 0, 0, 123456, tzinfo=UTC)
    assert monitoring_generation(_make_app(monitoring_changed_at=at)) == at.isoformat()


@pytest.mark.asyncio
async def test_recovering_incident_of_the_app_is_resolved_and_not_duplicated(mock_api_client):
    """Both active statuses are this application's incident, as the switch treats them."""
    from src.tasks import app_health_prober

    app_health_prober._consecutive_failures.clear()
    mock_api_client.list_active_incidents.return_value = [
        _make_incident(incident_id=5, status="recovering")
    ]
    with (
        patch.object(app_health_prober, "check_http_health", new_callable=AsyncMock) as http,
        patch.object(app_health_prober, "check_ssl_expiry", new_callable=AsyncMock) as ssl,
        patch.object(app_health_prober, "notify_admins_best_effort", new_callable=AsyncMock),
    ):
        ssl.return_value = None
        http.return_value = _FAIL
        await app_health_prober.check_application(
            app=_make_app(),
            server_ip="10.0.0.1",
            consecutive_failures=5,
            api_client=mock_api_client,
        )
        mock_api_client.create_incident.assert_not_called()

        http.return_value = _OK
        await app_health_prober.check_application(
            app=_make_app(),
            server_ip="10.0.0.1",
            consecutive_failures=0,
            api_client=mock_api_client,
            has_history=False,
        )
    mock_api_client.resolve_incident.assert_called_once_with(5, monitoring_generation="initial")
