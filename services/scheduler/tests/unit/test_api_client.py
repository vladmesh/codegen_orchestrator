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


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_update_task_sends_patch(self, api_client):
        """update_task sends PATCH to /api/tasks/{id} with given data."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "task-1", "current_iteration": 2}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        api_client._client = mock_http

        result = await api_client.update_task("task-1", {"current_iteration": 2})

        mock_http.request.assert_called_once_with(
            "PATCH",
            "/api/tasks/task-1",
            json={"current_iteration": 2},
        )
        assert result["current_iteration"] == 2


class TestFailStory:
    @pytest.mark.asyncio
    async def test_fail_story_posts_to_fail_endpoint(self, api_client):
        """fail_story sends POST to /api/stories/{id}/fail."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "story-1", "status": "failed"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        api_client._client = mock_http

        result = await api_client.fail_story("story-1")

        mock_http.request.assert_called_once_with(
            "POST",
            "/api/stories/story-1/fail",
            json={"actor": "supervisor"},
        )
        assert result["status"] == "failed"


class TestCreateIncident:
    @pytest.mark.asyncio
    async def test_create_incident_posts_correctly(self, api_client):
        """create_incident sends POST to /api/incidents/ with payload."""
        resp_data = {
            "id": 1,
            "server_handle": "vps-123",
            "incident_type": "server_unreachable",
            "status": "detected",
            "details": {"reason": "timeout"},
            "affected_services": [],
            "detected_at": "2026-03-17T00:00:00Z",
            "resolved_at": None,
            "recovery_attempts": 0,
        }
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
        assert result["id"] == 1
        assert result["incident_type"] == "server_unreachable"

    @pytest.mark.asyncio
    async def test_create_incident_with_affected_services(self, api_client):
        """create_incident forwards affected_services list."""
        resp_data = {"id": 2, "affected_services": ["web", "api"]}
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
        resp_data = [{"id": 1, "server_handle": "vps-123", "incident_type": "server_unreachable"}]
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
        resp_data = {"id": 1, "status": "resolved"}
        mock = _mock_http(resp_data)
        api_client._client = mock

        await api_client.resolve_incident(1)

        call_args = mock.request.call_args
        assert call_args[0] == ("PATCH", "/api/incidents/1")
        payload = call_args[1]["json"]
        assert payload["status"] == "resolved"
        assert "resolved_at" in payload


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
