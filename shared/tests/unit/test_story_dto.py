"""Unit tests for Story DTOs — StoryDTO, StoryCreate, StoryUpdate."""

from datetime import UTC, datetime
from typing import Any
import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.story import (
    StoryCreate,
    StoryDTO,
    StoryStatus,
    StoryType,
    StoryUpdate,
)

_NOW = datetime(2026, 3, 17, tzinfo=UTC)
_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestStoryDTO:
    """StoryDTO should parse API response dicts."""

    SAMPLE_RESPONSE: dict[str, Any] = {
        "id": "story-abc123",
        "project_id": str(_PROJECT_ID),
        "parent_story_id": "story-parent",
        "title": "User authentication",
        "description": "Implement login flow",
        "acceptance_criteria": "Users can log in",
        "type": "product",
        "status": "in_progress",
        "priority": 3,
        "blocked_by_story_id": "story-dep",
        "created_by": "po",
        "user_report": "Login button not visible",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_full_response(self):
        dto = StoryDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.id == "story-abc123"
        assert dto.project_id == _PROJECT_ID
        assert dto.parent_story_id == "story-parent"
        assert dto.title == "User authentication"
        assert dto.description == "Implement login flow"
        assert dto.acceptance_criteria == "Users can log in"
        assert dto.type == "product"
        assert dto.status == "in_progress"
        assert dto.priority == 3
        assert dto.blocked_by_story_id == "story-dep"
        assert dto.created_by == "po"
        assert dto.user_report == "Login button not visible"

    def test_parse_minimal_response(self):
        minimal = {
            "id": "story-min",
            "project_id": str(_PROJECT_ID),
            "parent_story_id": None,
            "title": "Simple story",
            "description": None,
            "acceptance_criteria": None,
            "type": "technical",
            "status": "created",
            "priority": 0,
            "blocked_by_story_id": None,
            "created_by": "system",
            "user_report": None,
            "created_at": _NOW.isoformat(),
        }
        dto = StoryDTO.model_validate(minimal)
        assert dto.id == "story-min"
        assert dto.parent_story_id is None
        assert dto.user_report is None
        assert dto.updated_at is None

    def test_model_dump_roundtrip(self):
        dto = StoryDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = StoryDTO.model_validate(data)
        assert dto2.id == dto.id

    def test_status_and_type_are_typed_enums(self):
        dto = StoryDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.status is StoryStatus.IN_PROGRESS
        assert dto.type is StoryType.PRODUCT

    def test_rejects_unknown_status(self):
        bad = {**self.SAMPLE_RESPONSE, "status": "review"}
        with pytest.raises(ValidationError):
            StoryDTO.model_validate(bad)

    def test_rejects_unknown_type(self):
        bad = {**self.SAMPLE_RESPONSE, "type": "epic"}
        with pytest.raises(ValidationError):
            StoryDTO.model_validate(bad)


class TestStoryCreate:
    """StoryCreate should serialize for API requests."""

    def test_minimal(self):
        create = StoryCreate(project_id=_PROJECT_ID, title="New story")
        data = create.model_dump(mode="json")
        assert data["project_id"] == str(_PROJECT_ID)
        assert data["title"] == "New story"
        assert data["type"] == "product"
        assert data["priority"] == 0

    def test_full(self):
        create = StoryCreate(
            project_id=_PROJECT_ID,
            title="Technical story",
            description="Refactor auth",
            acceptance_criteria="Tests pass",
            parent_story_id="story-parent",
            type=StoryType.TECHNICAL,
            priority=5,
            blocked_by_story_id="story-dep",
            created_by="architect",
        )
        data = create.model_dump(mode="json")
        assert data["type"] == "technical"
        assert data["blocked_by_story_id"] == "story-dep"


class TestStoryUpdate:
    """StoryUpdate should support partial updates."""

    def test_exclude_unset(self):
        update = StoryUpdate(title="New title", priority=10)
        data = update.model_dump(exclude_unset=True)
        assert data == {"title": "New title", "priority": 10}

    def test_all_fields_optional(self):
        update = StoryUpdate()
        data = update.model_dump(exclude_unset=True)
        assert data == {}
