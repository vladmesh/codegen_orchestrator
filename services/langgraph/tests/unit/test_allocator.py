"""Unit tests for resource allocation logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from tests.unit.factories import make_server

SERVER = make_server(
    handle="srv-1",
    status="ready",
    public_ip="1.2.3.4",
    capacity_ram_mb=4096,
    capacity_disk_mb=50000,
    last_health_check=datetime.now(UTC),
)

APP = {
    "id": 42,
    "repo_id": "repo-1",
    "server_handle": "srv-1",
    "service_name": "my-bot",
    "status": "running",
    "reserved_ram_mb": 512,
}


def _allocation_settings(*, reserve_mb: int = 256, freshness_seconds: int = 300):
    settings = type("Settings", (), {})()
    settings.allocation_ram_reserve_mb = reserve_mb
    settings.allocation_metrics_freshness_seconds = freshness_seconds
    return settings


def _fresh_server(**overrides):
    return make_server(last_health_check=datetime.now(UTC), **overrides)


class TestEnsureProjectAllocations:
    """Test ensure_project_allocations uses atomic endpoint."""

    @pytest.mark.asyncio
    async def test_creates_application_before_allocating(self):
        """Should create Application first, then allocate ports with application_id."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[])
        mock_client.get_or_create_application = AsyncMock(return_value=APP)
        mock_client.get_application_allocations = AsyncMock(return_value=[])
        mock_client.allocate_next_port = AsyncMock(
            return_value={
                "id": 1,
                "server_handle": "srv-1",
                "port": 8000,
                "service_name": "backend",
                "application_id": 42,
            }
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
            )

        # Application created before allocation
        mock_client.get_or_create_application.assert_called_once_with(
            repo_id="repo-1",
            server_handle="srv-1",
            service_name="my-bot",
            reserved_ram_mb=512,
        )
        mock_client.allocate_next_port.assert_called_once_with(
            "srv-1",
            {
                "service_name": "backend",
                "application_id": 42,
            },
        )
        assert len(result) == 1
        key = list(result.keys())[0]
        assert result[key]["port"] == 8000  # noqa: PLR2004
        assert result[key]["server_ip"] == "1.2.3.4"
        assert result[key]["application_id"] == 42  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_existing_allocations_returned_as_is(self):
        """When allocations already exist, should not call allocate_next_port."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=SERVER)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "port": 8001,
                    "server_ip": "1.2.3.4",
                    "service_name": "backend",
                }
            ]
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot"
            )

        mock_client.allocate_next_port.assert_not_called()
        mock_client.get_or_create_application.assert_not_called()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_existing_allocations_are_extended_for_missing_services(self):
        """Redeploy keeps persisted ports and allocates only newly required services."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=SERVER)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "server_ip": "1.2.3.4",
                    "port": 8000,
                    "service_name": "backend",
                }
            ]
        )
        mock_client.allocate_next_port.side_effect = [{"port": 8001}, {"port": 8002}]

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1",
                repo_id="repo-1",
                service_name="my-bot",
                modules=["backend", "postgres", "redis"],
            )

        assert result["srv-1:8000"]["service_name"] == "backend"
        assert result["srv-1:8001"]["service_name"] == "postgres"
        assert result["srv-1:8002"]["service_name"] == "redis"
        services = [
            call.args[1]["service_name"] for call in mock_client.allocate_next_port.await_args_list
        ]
        assert services == ["postgres", "redis"]

    @pytest.mark.asyncio
    async def test_redeploy_reuses_existing_placement_without_readmitting_ram(self):
        """A deployed project does not need another RAM budget to redeploy."""
        deployed_server = _fresh_server(capacity_ram_mb=1967, used_ram_mb=1300)
        mock_client = AsyncMock()
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=deployed_server)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "server_ip": "1.2.3.4",
                    "port": 8000,
                    "service_name": "backend",
                }
            ]
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot"
            )

        assert len(result) == 1
        mock_client.list_servers.assert_not_called()
        mock_client.get_or_create_application.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_modules_allocate_each(self):
        """Each module should get its own atomic allocation."""
        call_count = 0

        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[])
        mock_client.get_or_create_application = AsyncMock(return_value=APP)
        mock_client.get_application_allocations = AsyncMock(return_value=[])

        async def _allocate_next(handle, payload):
            nonlocal call_count
            call_count += 1
            return {
                "id": call_count,
                "server_handle": handle,
                "port": 7999 + call_count,
                "service_name": payload["service_name"],
                "application_id": payload["application_id"],
            }

        mock_client.allocate_next_port = _allocate_next

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1",
                repo_id="repo-1",
                service_name="my-bot",
                modules=["backend", "frontend"],
            )

        assert call_count == 2  # noqa: PLR2004
        assert len(result) == 2  # noqa: PLR2004


class TestSuitableServer:
    """Server selection uses reservations and fresh observed memory together."""

    @pytest.mark.asyncio
    async def test_rejects_server_with_insufficient_observed_free_memory(self):
        server = _fresh_server(capacity_ram_mb=4096, used_ram_mb=3600)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_free_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_rejects_server_with_stale_metrics(self):
        server = make_server(last_health_check=datetime.now(UTC) - timedelta(minutes=10))
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="no_fresh_metrics"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_marks_request_larger_than_every_server_as_impossible(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=0)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="impossible_capacity"):
                await _find_suitable_server(1024, 1024)

    @pytest.mark.asyncio
    async def test_uses_worse_of_reserved_and_observed_memory(self):
        server = _fresh_server(capacity_ram_mb=4500, used_ram_mb=4000)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 3500, "status": "running"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_free_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_reports_capacity_when_no_server_can_fit_reservations(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=100)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 512, "status": "running"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_reserved_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_returns_candidate_with_most_remaining_memory(self):
        smaller = _fresh_server(handle="smaller", capacity_ram_mb=2048, used_ram_mb=600)
        larger = _fresh_server(handle="larger", capacity_ram_mb=4096, used_ram_mb=1000)
        client = AsyncMock()
        client.list_servers.return_value = [smaller, larger]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import _find_suitable_server

            selected = await _find_suitable_server(512, 1024)

        assert selected.handle == "larger"

    @pytest.mark.asyncio
    async def test_ignores_reservation_from_undeployed_application(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=100)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 512, "status": "not_deployed"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import _find_suitable_server

            selected = await _find_suitable_server(512, 1024)

        assert selected is server
