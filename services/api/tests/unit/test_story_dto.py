"""Unit tests for Story DTO — enums and valid transitions."""

import pytest

from shared.contracts.dto.story import VALID_TRANSITIONS, StoryStatus


class TestStoryStatus:
    def test_values(self):
        assert StoryStatus.CREATED == "created"
        assert StoryStatus.IN_PROGRESS == "in_progress"
        assert StoryStatus.COMPLETED == "completed"
        assert StoryStatus.ARCHIVED == "archived"

    def test_membership(self):
        values = list(StoryStatus)
        assert len(values) == 4  # noqa: PLR2004
        assert "created" in values
        assert "in_progress" in values
        assert "completed" in values
        assert "archived" in values


class TestStoryTransitions:
    def test_created_can_start(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_created_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_in_progress_can_complete(self):
        assert StoryStatus.COMPLETED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_in_progress_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_completed_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.COMPLETED]

    def test_completed_can_reopen(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.COMPLETED]

    def test_archived_no_transitions(self):
        assert VALID_TRANSITIONS[StoryStatus.ARCHIVED] == set()

    def test_invalid_transition_created_to_completed(self):
        assert StoryStatus.COMPLETED not in VALID_TRANSITIONS[StoryStatus.CREATED]

    @pytest.mark.parametrize("status", list(StoryStatus))
    def test_all_statuses_have_transitions(self, status):
        assert status in VALID_TRANSITIONS
