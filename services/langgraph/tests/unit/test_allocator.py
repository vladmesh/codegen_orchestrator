"""Unit tests for resource allocation logic."""

from unittest.mock import AsyncMock, patch

import pytest

SERVER = {
    "handle": "srv-1",
    "status": "ready",
    "public_ip": "1.2.3.4",
    "capacity_ram_mb": 4096,
    "capacity_disk_mb": 50000,
}

APP = {"id": 42, "repo_id": "repo-1", "server_handle": "srv-1", "service_name": "my-bot"}


class TestEnsureProjectAllocations:
    """Test ensure_project_allocations uses atomic endpoint."""

    @pytest.mark.asyncio
    async def test_creates_application_before_allocating(self):
        """Should create Application first, then allocate ports with application_id."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
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

        with patch("src.tools.allocator.api_client", mock_client):
            from src.tools.allocator import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
            )

        # Application created before allocation
        mock_client.get_or_create_application.assert_called_once_with(
            repo_id="repo-1",
            server_handle="srv-1",
            service_name="my-bot",
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
        mock_client.get_or_create_application = AsyncMock(return_value=APP)
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

        with patch("src.tools.allocator.api_client", mock_client):
            from src.tools.allocator import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot"
            )

        mock_client.allocate_next_port.assert_not_called()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_multiple_modules_allocate_each(self):
        """Each module should get its own atomic allocation."""
        call_count = 0

        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
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

        with patch("src.tools.allocator.api_client", mock_client):
            from src.tools.allocator import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1",
                repo_id="repo-1",
                service_name="my-bot",
                modules=["backend", "frontend"],
            )

        assert call_count == 2  # noqa: PLR2004
        assert len(result) == 2  # noqa: PLR2004
