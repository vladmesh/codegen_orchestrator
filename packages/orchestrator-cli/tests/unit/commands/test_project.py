from unittest.mock import AsyncMock, patch
import uuid

import pytest
from typer.testing import CliRunner

from shared.contracts.dto.project import ProjectDTO, ProjectStatus

runner = CliRunner()


@pytest.fixture
def mock_api_client():
    with patch("orchestrator_cli.commands.project.get_api_client") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client


@pytest.fixture
def mock_redis_client():
    with patch("orchestrator_cli.commands.project.get_redis_client") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client


@pytest.mark.asyncio
async def test_create_project_success(mock_api_client, mock_redis_client):
    """Test successful project creation with dual-write"""
    # Setup mocks
    fixed_uuid = "00000000-0000-0000-0000-000000000000"

    with patch("orchestrator_cli.commands.project.uuid.uuid4", return_value=uuid.UUID(fixed_uuid)):
        project_dto = ProjectDTO(
            id=fixed_uuid, name="test-project", status=ProjectStatus.DRAFT, modules=[]
        )

        # API mock returns DTO dict
        mock_api_client.post.return_value.json.return_value = project_dto.model_dump()
        mock_api_client.post.return_value.status_code = 201

        from orchestrator_cli.commands.project import create_project_command

        await create_project_command(name="test-project")

        # Verify API called
        mock_api_client.post.assert_called_once()
        assert mock_api_client.post.call_args[0][0] == "/api/projects/"
        assert mock_api_client.post.call_args[1]["json"]["name"] == "test-project"

        # Verify Redis called
        mock_redis_client.xadd.assert_called_once()
        assert mock_redis_client.xadd.call_args[1]["name"] == "scaffolder:queue"
        # Check payload has project_id
        payload = mock_redis_client.xadd.call_args[1]["fields"]
        assert "project_id" in payload
        assert payload["project_id"] == fixed_uuid


@pytest.mark.asyncio
async def test_create_project_api_fail(mock_api_client, mock_redis_client):
    """Test API failure aborts Redis write"""
    # Setup mocks
    mock_api_client.post.side_effect = Exception("API Error")

    from orchestrator_cli.commands.project import create_project_command

    with pytest.raises(Exception, match="API Error"):
        await create_project_command(name="fail-project")

    # Verify API called
    mock_api_client.post.assert_called_once()

    # Verify Redis NOT called
    mock_redis_client.xadd.assert_not_called()
