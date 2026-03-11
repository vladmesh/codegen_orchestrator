"""Unit tests for Story DTO — enums and valid transitions."""

import pytest

from shared.contracts.dto.story import VALID_TRANSITIONS, StoryStatus, StoryType


class TestStoryStatus:
    def test_values(self):
        assert StoryStatus.CREATED == "created"
        assert StoryStatus.IN_PROGRESS == "in_progress"
        assert StoryStatus.COMPLETED == "completed"
        assert StoryStatus.ARCHIVED == "archived"

    def test_failed_value(self):
        assert StoryStatus.FAILED == "failed"

    def test_membership(self):
        values = list(StoryStatus)
        assert len(values) == 6  # noqa: PLR2004
        assert "created" in values
        assert "in_progress" in values
        assert "deploying" in values
        assert "completed" in values
        assert "archived" in values
        assert "failed" in values


class TestStoryTransitions:
    def test_created_can_start(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_created_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_in_progress_can_deploy(self):
        assert StoryStatus.DEPLOYING in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_in_progress_can_complete(self):
        assert StoryStatus.COMPLETED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_deploying_can_complete(self):
        assert StoryStatus.COMPLETED in VALID_TRANSITIONS[StoryStatus.DEPLOYING]

    def test_deploying_can_rollback(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.DEPLOYING]

    def test_deploying_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.DEPLOYING]

    def test_in_progress_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_completed_can_archive(self):
        assert StoryStatus.ARCHIVED in VALID_TRANSITIONS[StoryStatus.COMPLETED]

    def test_completed_can_reopen(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.COMPLETED]

    def test_archived_no_transitions(self):
        assert VALID_TRANSITIONS[StoryStatus.ARCHIVED] == set()

    def test_created_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_in_progress_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_failed_no_transitions(self):
        assert VALID_TRANSITIONS[StoryStatus.FAILED] == set()

    def test_invalid_transition_created_to_completed(self):
        assert StoryStatus.COMPLETED not in VALID_TRANSITIONS[StoryStatus.CREATED]

    @pytest.mark.parametrize("status", list(StoryStatus))
    def test_all_statuses_have_transitions(self, status):
        assert status in VALID_TRANSITIONS


class TestStoryType:
    def test_values(self):
        assert StoryType.PRODUCT == "product"
        assert StoryType.TECHNICAL == "technical"

    def test_membership(self):
        values = list(StoryType)
        assert len(values) == 2  # noqa: PLR2004
        assert "product" in values
        assert "technical" in values
