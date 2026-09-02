"""Unit tests for Story API schemas — validation, defaults, from_attributes."""

from datetime import UTC, datetime
from typing import get_type_hints
import uuid

from pydantic import BaseModel, ValidationError
import pytest

from shared.contracts.dto.story import StoryDTO, StoryWaitingOn
from src.schemas.story import StoryAccept, StoryCreate, StoryRead, StoryReopen, StoryUpdate

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

#: Sentinel for "this field has no default", so a field defaulting to `None` and
#: a required field never compare equal.
_NO_DEFAULT = object()


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
        mock.waiting_on = "none"
        mock.priority = 5
        mock.blocked_by_story_id = "story-blocker"
        mock.created_by = "po"
        mock.type = "technical"
        mock.user_report = None
        mock.quarantine_reason = None
        mock.operator_acceptance = None
        mock.operator_recheck = None
        mock.reopened_at = None
        mock.owner_notification = None
        mock.created_at = now
        mock.updated_at = now

        r = StoryRead.model_validate(mock, from_attributes=True)
        assert r.id == "story-abc123"
        assert r.status == "created"
        assert r.waiting_on == "none"
        assert r.type == "technical"
        assert r.priority == 5
        assert r.blocked_by_story_id == "story-blocker"
        assert r.user_report is None
        assert r.quarantine_reason is None

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
        mock.waiting_on = "none"
        mock.priority = 0
        mock.blocked_by_story_id = None
        mock.created_by = "po"
        mock.type = "product"
        mock.user_report = "Images still broken on mobile"
        mock.quarantine_reason = None
        mock.operator_acceptance = None
        mock.operator_recheck = None
        mock.reopened_at = None
        mock.owner_notification = None
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


class TestStoryAccept:
    def test_strips_a_nonblank_basis(self):
        assert StoryAccept(basis="  verified manually  ").basis == "verified manually"

    def test_refuses_blank_basis(self):
        with pytest.raises(ValidationError):
            StoryAccept(basis="   ")


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


class TestStoryReadPairing:
    """`StoryRead` and the shared `StoryDTO` are one response contract in two files.

    Every client that reads a story (`services/scheduler`, `services/langgraph`,
    `services/scaffolder`) parses the response through `StoryDTO`, which ignores
    fields it does not declare and applies its own defaults to fields the
    response omits.  A field that exists on one model alone, or is typed
    differently, or is required on one side and optional on the other, is a
    contract split that no runtime fallback may paper over — so the whole field
    spec is compared here, in the fast suite, rather than discovered downstream.
    """

    @staticmethod
    def _field_specs(model: type[BaseModel]) -> dict[str, tuple[object, bool, object]]:
        """Name -> (annotation, required?, default) for every field of `model`.

        Annotations come from `get_type_hints` so a forward reference and the
        class it names compare equal; nothing else is normalised.
        """
        hints = get_type_hints(model)

        def default_of(field) -> object:
            if field.is_required():
                return _NO_DEFAULT
            return field.get_default(call_default_factory=True)

        return {
            name: (hints[name], field.is_required(), default_of(field))
            for name, field in model.model_fields.items()
        }

    def test_both_models_declare_the_same_fields(self):
        assert set(StoryDTO.model_fields) == set(StoryRead.model_fields)

    def test_both_models_declare_the_same_field_spec(self):
        """Annotation, requiredness and default, field by field.

        The name set alone let `waiting_on` be required on `StoryRead` while
        `StoryDTO` defaulted it to `NONE`, which turned a response missing the
        field into an invented "not waiting".  Comparing the whole spec is what
        makes that drift impossible to land.
        """
        assert self._field_specs(StoryDTO) == self._field_specs(StoryRead)

    def test_the_wait_is_required_on_both_sides(self):
        """Spelled out, because this is the field the contract split twice on."""
        for model in (StoryDTO, StoryRead):
            field = model.model_fields["waiting_on"]
            assert get_type_hints(model)["waiting_on"] is StoryWaitingOn
            assert field.is_required(), f"{model.__name__}.waiting_on must have no default"


class TestStoryDTORefusesAMissingWait:
    def test_a_story_response_without_waiting_on_raises(self):
        """No producer may omit it, so an omission is a broken response.

        The column is non-nullable, migration `c3f7a91d2b48` backfilled every
        existing row, and `StoryRead` always returns the field.
        """
        response = {
            "id": "story-abc123",
            "project_id": str(PROJECT_UUID),
            "title": "User login",
            "type": "product",
            "status": "pr_review",
            "priority": 0,
            "created_by": "po",
            "created_at": datetime.now(UTC).isoformat(),
        }

        with pytest.raises(ValidationError) as excinfo:
            StoryDTO.model_validate(response)

        assert any(error["loc"] == ("waiting_on",) for error in excinfo.value.errors())
