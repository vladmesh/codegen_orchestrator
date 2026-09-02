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
    StoryWaitingOn,
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
        "waiting_on": "ci",
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
        assert dto.waiting_on is StoryWaitingOn.CI

    def test_parse_response_without_the_optional_fields(self):
        """The genuinely optional fields may be absent; `waiting_on` is not one."""
        minimal = {
            "id": "story-min",
            "project_id": str(_PROJECT_ID),
            "title": "Simple story",
            "type": "technical",
            "status": "created",
            "waiting_on": "none",
            "priority": 0,
            "created_by": "system",
            "created_at": _NOW.isoformat(),
        }
        dto = StoryDTO.model_validate(minimal)
        assert dto.id == "story-min"
        assert dto.parent_story_id is None
        assert dto.user_report is None
        assert dto.updated_at is None
        assert dto.waiting_on is StoryWaitingOn.NONE

    def test_a_response_that_omits_waiting_on_is_refused(self):
        """No default may interpret a missing field into an invented `none`.

        The column is non-nullable, migration `c3f7a91d2b48` backfilled every
        existing row and `StoryRead` always returns the field, so there is no
        producer that may legitimately omit it.  A response without it is a
        broken response and must raise here rather than be read as "not
        waiting" and then re-published as a fact about the story.
        """
        without_wait = {k: v for k, v in self.SAMPLE_RESPONSE.items() if k != "waiting_on"}

        with pytest.raises(ValidationError) as excinfo:
            StoryDTO.model_validate(without_wait)

        assert any(error["loc"] == ("waiting_on",) for error in excinfo.value.errors())

    def test_model_dump_roundtrip(self):
        dto = StoryDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = StoryDTO.model_validate(data)
        assert dto2.id == dto.id
        # The field survives serialization, so it reaches every consumer that
        # re-dumps a story it parsed.
        assert data["waiting_on"] == "ci"
        assert dto2.waiting_on is StoryWaitingOn.CI

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

    def test_rejects_unknown_waiting_on(self):
        bad = {**self.SAMPLE_RESPONSE, "waiting_on": "weather"}
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
