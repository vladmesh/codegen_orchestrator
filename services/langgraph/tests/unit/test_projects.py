"""Tests for project tools."""

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.projects import validate_project_name


class TestValidateProjectName:
    """Tests for project name validation."""

    def test_valid_simple_name(self):
        """Valid simple lowercase name passes."""
        validate_project_name("myproject")

    def test_valid_with_numbers(self):
        """Valid name with numbers passes."""
        validate_project_name("project123")

    def test_valid_with_hyphens(self):
        """Valid name with hyphens passes."""
        validate_project_name("my-cool-project")

    def test_valid_complex(self):
        """Valid complex name passes."""
        validate_project_name("weather-bot-v2")

    def test_invalid_uppercase(self):
        """Uppercase letters are rejected."""
        with pytest.raises(ValueError, match="lowercase"):
            validate_project_name("MyProject")

    def test_invalid_starts_with_number(self):
        """Names starting with number are rejected."""
        with pytest.raises(ValueError, match="start with a letter"):
            validate_project_name("123project")

    def test_invalid_underscores(self):
        """Underscores are rejected (not kebab-case)."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my_project")

    def test_invalid_spaces(self):
        """Spaces are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my project")

    def test_invalid_special_chars(self):
        """Special characters are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("my@project!")

    def test_invalid_cyrillic(self):
        """Cyrillic characters are rejected."""
        with pytest.raises(ValueError, match="letters, numbers, and hyphens"):
            validate_project_name("мойпроект")

    def test_invalid_empty(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError):
            validate_project_name("")

    def test_invalid_starts_with_hyphen(self):
        """Names starting with hyphen are rejected."""
        with pytest.raises(ValueError, match="start with a letter"):
            validate_project_name("-myproject")


class TestUpdateProject:
    """Tests for update_project tool."""

    @pytest.mark.asyncio
    async def test_update_repository_url(self):
        """Test updating repository URL."""
        with patch("src.tools.projects.api_client") as mock_client:
            # Mock successful response matching ProjectInfo schema
            mock_client.patch = AsyncMock(
                return_value={
                    "id": "p1",
                    "name": "test",
                    "status": "active",
                    "repo_url": "new-url",  # API might return this if formatted for ProjectInfo
                }
            )
            mock_client.get = AsyncMock()

            from src.tools.projects import update_project

            result = await update_project.ainvoke({"project_id": "p1", "repository_url": "new-url"})

            mock_client.patch.assert_called_once()
            call_args = mock_client.patch.call_args
            assert call_args[0][0] == "/projects/p1"
            assert call_args[1]["json"] == {"repository_url": "new-url"}
            # ProjectInfo mapping depends on API response. We mock response to have repo_url.
            assert result.repo_url == "new-url"

    @pytest.mark.asyncio
    async def test_update_status(self):
        """Test updating project status."""
        with patch("src.tools.projects.api_client") as mock_client:
            mock_client.patch = AsyncMock(
                return_value={"id": "p1", "name": "test", "status": "maintenance"}
            )
            mock_client.get = AsyncMock()

            from src.tools.projects import update_project

            result = await update_project.ainvoke({"project_id": "p1", "status": "maintenance"})

            mock_client.patch.assert_called_once()
            assert mock_client.patch.call_args[1]["json"] == {"status": "maintenance"}
            assert result.status == "maintenance"

    @pytest.mark.asyncio
    async def test_no_update(self):
        """Test no update if no fields provided."""
        with patch("src.tools.projects.api_client") as mock_client:
            mock_client.get = AsyncMock(
                return_value={"id": "p1", "name": "test", "status": "active"}
            )
            mock_client.patch = AsyncMock()

            from src.tools.projects import update_project

            await update_project.ainvoke({"project_id": "p1"})

            mock_client.patch.assert_not_called()
            mock_client.get.assert_called_once()
