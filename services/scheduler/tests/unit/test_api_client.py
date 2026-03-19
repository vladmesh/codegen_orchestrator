"""Unit tests for SchedulerAPIClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def api_client():
    with patch("src.config.get_settings") as mock_settings:
        settings = MagicMock()
        settings.api_base_url = "http://localhost:8000"
        mock_settings.return_value = settings
        from src.clients.api import SchedulerAPIClient

        return SchedulerAPIClient()


def _mock_http(response_data: dict | list, status_code: int = 200) -> AsyncMock:
    """Create a mock httpx.AsyncClient with a preset response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_data
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = status_code
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False
    return mock_client


def _task_data(**overrides) -> dict:
    """Build a minimal valid task API response."""
    base = {
        "id": "task-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "type": "feature",
        "title": "Test task",
        "status": "backlog",
        "priority": 0,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "created_at": "2026-03-17T00:00:00Z",
        "updated_at": "2026-03-17T00:00:00Z",
    }
    base.update(overrides)
    return base


def _story_data(**overrides) -> dict:
    """Build a minimal valid story API response."""
    base = {
        "id": "story-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "title": "Test story",
        "type": "product",
        "status": "created",
        "priority": 0,
        "created_by": "system",
        "created_at": "2026-03-17T00:00:00Z",
        "updated_at": "2026-03-17T00:00:00Z",
    }
    base.update(overrides)
    return base


def _incident_data(**overrides) -> dict:
    """Build a minimal valid incident API response."""
    base = {
        "id": 1,
        "server_handle": "vps-123",
        "incident_type": "server_unreachable",
        "status": "detected",
        "detected_at": "2026-03-17T00:00:00Z",
        "resolved_at": None,
        "details": {},
        "affected_services": [],
        "recovery_attempts": 0,
        "created_at": "2026-03-17T00:00:00Z",
        "updated_at": "2026-03-17T00:00:00Z",
    }
    base.update(overrides)
    return base


def _app_data(**overrides) -> dict:
    """Build a minimal valid application API response."""
    base = {
        "id": 1,
        "repo_id": "repo-1",
        "server_handle": "vps-1",
        "service_name": "web",
        "status": "running",
        "created_at": "2026-03-17T00:00:00Z",
        "updated_at": "2026-03-17T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_update_task_sends_patch(self, api_client):
        """update_task sends PATCH to /api/tasks/{id} with given data."""
        mock = _mock_http(_task_data(current_iteration=2))
        api_client._client = mock

        result = await api_client.update_task("task-1", {"current_iteration": 2})

        mock.request.assert_called_once_with(
            "PATCH",
            "/api/tasks/task-1",
            json={"current_iteration": 2},
        )
        assert result.current_iteration == 2


class TestFailStory:
    @pytest.mark.asyncio
    async def test_fail_story_posts_to_fail_endpoint(self, api_client):
        """fail_story sends POST to /api/stories/{id}/fail."""
        mock = _mock_http(_story_data(status="failed"))
        api_client._client = mock

        result = await api_client.fail_story("story-1")

        mock.request.assert_called_once_with(
            "POST",
            "/api/stories/story-1/fail",
            json={"actor": "supervisor"},
        )
        assert result.status == "failed"


class TestCreateIncident:
    @pytest.mark.asyncio
    async def test_create_incident_posts_correctly(self, api_client):
        """create_incident sends POST to /api/incidents/ with payload."""
        resp_data = _incident_data(details={"reason": "timeout"})
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.create_incident(
            server_handle="vps-123",
            incident_type="server_unreachable",
            details={"reason": "timeout"},
        )

        mock.request.assert_called_once_with(
            "POST",
            "/api/incidents/",
            json={
                "server_handle": "vps-123",
                "incident_type": "server_unreachable",
                "details": {"reason": "timeout"},
                "affected_services": [],
            },
        )
        assert result.id == 1
        assert result.incident_type == "server_unreachable"

    @pytest.mark.asyncio
    async def test_create_incident_with_affected_services(self, api_client):
        """create_incident forwards affected_services list."""
        resp_data = _incident_data(id=2, affected_services=["web", "api"])
        mock = _mock_http(resp_data)
        api_client._client = mock

        await api_client.create_incident(
            server_handle="vps-123",
            incident_type="resource_exhausted",
            details={},
            affected_services=["web", "api"],
        )

        call_kwargs = mock.request.call_args
        assert call_kwargs[1]["json"]["affected_services"] == ["web", "api"]


class TestGetActiveIncidents:
    @pytest.mark.asyncio
    async def test_get_active_incidents_filters_correctly(self, api_client):
        """get_active_incidents queries with server_handle, type, and active status."""
        resp_data = [_incident_data()]
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.get_active_incidents("vps-123", "server_unreachable")

        mock.request.assert_called_once_with(
            "GET",
            "/api/incidents/",
            params={
                "server_handle": "vps-123",
                "incident_type": "server_unreachable",
                "status": "detected",
            },
        )
        assert len(result) == 1
        assert result[0].id == 1

    @pytest.mark.asyncio
    async def test_get_active_incidents_returns_empty_list(self, api_client):
        """get_active_incidents returns empty list when none found."""
        mock = _mock_http([])
        api_client._client = mock

        result = await api_client.get_active_incidents("vps-123", "server_unreachable")
        assert result == []


class TestResolveIncident:
    @pytest.mark.asyncio
    async def test_resolve_incident_patches_correctly(self, api_client):
        """resolve_incident sends PATCH with resolved status and timestamp."""
        resp_data = _incident_data(status="resolved")
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.resolve_incident(1)

        call_args = mock.request.call_args
        assert call_args[0] == ("PATCH", "/api/incidents/1")
        payload = call_args[1]["json"]
        assert payload["status"] == "resolved"
        assert "resolved_at" in payload
        assert result.status == "resolved"


class TestCreateMetricsHistory:
    @pytest.mark.asyncio
    async def test_create_metrics_history_posts_correctly(self, api_client):
        """create_metrics_history sends POST to /api/servers/{handle}/metrics-history."""
        resp_data = {"id": 1, "server_handle": "vps-123", "metrics": {"cpu": 50.0}}
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.create_metrics_history("vps-123", {"cpu": 50.0})

        mock.request.assert_called_once_with(
            "POST",
            "/api/servers/vps-123/metrics-history",
            json={"metrics": {"cpu": 50.0}},
        )
        assert result["server_handle"] == "vps-123"


class TestDeleteOldMetricsHistory:
    @pytest.mark.asyncio
    async def test_delete_old_metrics_history(self, api_client):
        """delete_old_metrics_history sends DELETE with retention_hours param."""
        resp_data = {"deleted": 42}
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.delete_old_metrics_history(168)

        mock.request.assert_called_once_with(
            "DELETE",
            "/api/servers/metrics-history",
            params={"retention_hours": 168},
        )
        assert result["deleted"] == 42


class TestGetApplications:
    @pytest.mark.asyncio
    async def test_get_applications_no_filters(self, api_client):
        """get_applications with no filters sends GET to /api/applications/."""
        resp_data = [_app_data()]
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.get_applications()

        mock.request.assert_called_once_with(
            "GET",
            "/api/applications/",
            params={},
        )
        assert len(result) == 1
        assert result[0].service_name == "web"

    @pytest.mark.asyncio
    async def test_get_applications_with_filters(self, api_client):
        """get_applications passes server_handle and status as query params."""
        mock = _mock_http([])
        api_client._client = mock

        await api_client.get_applications(server_handle="vps-1", status="running")

        mock.request.assert_called_once_with(
            "GET",
            "/api/applications/",
            params={"server_handle": "vps-1", "status": "running"},
        )


class TestUpdateApplication:
    @pytest.mark.asyncio
    async def test_update_application_sends_patch(self, api_client):
        """update_application sends PATCH to /api/applications/{id}."""
        resp_data = _app_data(response_time_ms=45)
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.update_application(1, {"response_time_ms": 45})

        mock.request.assert_called_once_with(
            "PATCH",
            "/api/applications/1",
            json={"response_time_ms": 45},
        )
        assert result.response_time_ms == 45


class TestCreateAppHealthHistory:
    @pytest.mark.asyncio
    async def test_create_app_health_history(self, api_client):
        """create_app_health_history sends POST to /api/applications/{id}/health-history."""
        resp_data = {"id": 1, "application_id": 5, "metrics": {"healthy": True}}
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.create_app_health_history(5, {"healthy": True})

        mock.request.assert_called_once_with(
            "POST",
            "/api/applications/5/health-history",
            json={"metrics": {"healthy": True}},
        )
        assert result["application_id"] == 5


class TestDeleteOldAppHealthHistory:
    @pytest.mark.asyncio
    async def test_delete_old_app_health_history(self, api_client):
        """delete_old_app_health_history sends DELETE with retention_hours."""
        resp_data = {"deleted": 15}
        mock = _mock_http(resp_data)
        api_client._client = mock

        result = await api_client.delete_old_app_health_history(168)

        mock.request.assert_called_once_with(
            "DELETE",
            "/api/applications/health-history",
            params={"retention_hours": 168},
        )
        assert result["deleted"] == 15
