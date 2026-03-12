"""Unit tests for Story API schemas — validation, defaults, from_attributes."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from src.schemas.story import StoryCreate, StoryRead, StoryReopen, StoryUpdate

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestStoryCreate:
    def test_minimal(self):
        s = StoryCreate(project_id=PROJECT_UUID, title="User login")
        assert s.project_id == PROJECT_UUID
        assert s.title == "User login"
        assert s.description is None
        assert s.acceptance_criteria is None
        assert s.parent_story_id is None
        assert s.priority == 0
        assert s.blocked_by_story_id is None
        assert s.created_by == "system"
        assert s.type == "product"

    def test_technical_type(self):
        s = StoryCreate(project_id=PROJECT_UUID, title="Rust migration", type="technical")
        assert s.type == "technical"

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            StoryCreate(project_id=PROJECT_UUID, title="Bad", type="invalid")

    def test_all_fields(self):
        s = StoryCreate(
            project_id=PROJECT_UUID,
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
            StoryCreate(project_id=PROJECT_UUID)


class TestStoryRead:
    def test_from_attributes(self):
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        mock = MagicMock()
        mock.id = "story-abc123"
        mock.project_id = PROJECT_UUID
        mock.parent_story_id = None
        mock.title = "User login"
        mock.description = "Details"
        mock.acceptance_criteria = None
        mock.status = "created"
        mock.priority = 5
        mock.blocked_by_story_id = "story-blocker"
        mock.created_by = "po"
        mock.type = "technical"
        mock.user_report = None
        mock.created_at = now
        mock.updated_at = now

        r = StoryRead.model_validate(mock, from_attributes=True)
        assert r.id == "story-abc123"
        assert r.status == "created"
        assert r.type == "technical"
        assert r.priority == 5
        assert r.blocked_by_story_id == "story-blocker"
        assert r.user_report is None

    def test_from_attributes_with_user_report(self):
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        mock = MagicMock()
        mock.id = "story-abc123"
        mock.project_id = PROJECT_UUID
        mock.parent_story_id = None
        mock.title = "Fix images"
        mock.description = None
        mock.acceptance_criteria = None
        mock.status = "in_progress"
        mock.priority = 0
        mock.blocked_by_story_id = None
        mock.created_by = "po"
        mock.type = "product"
        mock.user_report = "Images still broken on mobile"
        mock.created_at = now
        mock.updated_at = now

        r = StoryRead.model_validate(mock, from_attributes=True)
        assert r.user_report == "Images still broken on mobile"


class TestStoryReopen:
    def test_defaults(self):
        r = StoryReopen()
        assert r.user_report is None
        assert r.actor == "system"

    def test_with_user_report(self):
        r = StoryReopen(user_report="Images broken", actor="po")
        assert r.user_report == "Images broken"
        assert r.actor == "po"


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
            priority=3,
            blocked_by_story_id="story-dep",
        )
        data = u.model_dump(exclude_unset=True)
        assert len(data) == 6  # noqa: PLR2004
        assert data["priority"] == 3
        assert data["blocked_by_story_id"] == "story-dep"
