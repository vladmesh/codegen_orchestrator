"""Unit tests for Story API schemas — validation, defaults, from_attributes."""

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from src.schemas.story import StoryCreate, StoryRead, StoryUpdate


class TestStoryCreate:
    def test_minimal(self):
        s = StoryCreate(project_id="proj-1", title="User login")
        assert s.project_id == "proj-1"
        assert s.title == "User login"
        assert s.description is None
        assert s.acceptance_criteria is None
        assert s.parent_story_id is None
        assert s.created_by == "system"

    def test_all_fields(self):
        s = StoryCreate(
            project_id="proj-1",
            title="User login",
            description="Allow users to log in",
            acceptance_criteria="Login form works",
            parent_story_id="story-parent",
            created_by="po",
        )
        assert s.description == "Allow users to log in"
        assert s.parent_story_id == "story-parent"
        assert s.created_by == "po"

    def test_missing_required_project_id(self):
        with pytest.raises(ValidationError):
            StoryCreate(title="No project")

    def test_missing_required_title(self):
        with pytest.raises(ValidationError):
            StoryCreate(project_id="proj-1")


class TestStoryRead:
    def test_from_attributes(self):
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        mock = MagicMock()
        mock.id = "story-abc123"
        mock.project_id = "proj-1"
        mock.parent_story_id = None
        mock.title = "User login"
        mock.description = "Details"
        mock.acceptance_criteria = None
        mock.status = "created"
        mock.created_by = "po"
        mock.created_at = now
        mock.updated_at = now

        r = StoryRead.model_validate(mock, from_attributes=True)
        assert r.id == "story-abc123"
        assert r.status == "created"


class TestStoryUpdate:
    def test_partial(self):
        u = StoryUpdate(title="New title")
        data = u.model_dump(exclude_unset=True)
        assert data == {"title": "New title"}

    def test_empty(self):
        u = StoryUpdate()
        data = u.model_dump(exclude_unset=True)
        assert data == {}

    def test_all_fields(self):
        u = StoryUpdate(
            title="New",
            description="Desc",
            acceptance_criteria="AC",
            parent_story_id="story-parent",
        )
        data = u.model_dump(exclude_unset=True)
        assert len(data) == 4  # noqa: PLR2004
