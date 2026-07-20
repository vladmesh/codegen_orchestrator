"""Unit tests for Story DTO — enums and valid transitions."""

import pytest

from shared.contracts.dto.story import VALID_TRANSITIONS, StoryStatus, StoryType


class TestStoryStatus:
    def test_values(self):
        assert StoryStatus.CREATED == "created"
        assert StoryStatus.IN_PROGRESS == "in_progress"
        assert StoryStatus.REOPENED == "reopened"
        assert StoryStatus.COMPLETED == "completed"
        assert StoryStatus.ARCHIVED == "archived"

    def test_failed_value(self):
        assert StoryStatus.FAILED == "failed"

    def test_testing_value(self):
        assert StoryStatus.TESTING == "testing"

    def test_membership(self):
        values = list(StoryStatus)
        assert len(values) == 11  # noqa: PLR2004
        assert "created" in values
        assert "in_progress" in values
        assert "reopened" in values
        assert "deploying" in values
        assert "testing" in values
        assert "completed" in values
        assert "archived" in values
        assert "failed" in values
        assert "waiting_human_review" in values
        assert "waiting_user_secret" in values


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
        assert StoryStatus.REOPENED in VALID_TRANSITIONS[StoryStatus.COMPLETED]

    def test_archived_no_transitions(self):
        assert VALID_TRANSITIONS[StoryStatus.ARCHIVED] == set()

    def test_created_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_in_progress_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_failed_can_reopen(self):
        assert StoryStatus.REOPENED in VALID_TRANSITIONS[StoryStatus.FAILED]

    def test_reopened_can_start(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.REOPENED]

    def test_reopened_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.REOPENED]

    def test_waiting_human_review_status_value(self):
        assert StoryStatus.WAITING_HUMAN_REVIEW == "waiting_human_review"

    def test_in_progress_can_go_to_whr(self):
        assert StoryStatus.WAITING_HUMAN_REVIEW in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_whr_transitions(self):
        allowed = VALID_TRANSITIONS[StoryStatus.WAITING_HUMAN_REVIEW]
        assert StoryStatus.IN_PROGRESS in allowed  # admin resolves
        assert StoryStatus.FAILED in allowed  # admin gives up
        assert StoryStatus.COMPLETED not in allowed  # can't skip to completed

    def test_invalid_transition_created_to_completed(self):
        assert StoryStatus.COMPLETED not in VALID_TRANSITIONS[StoryStatus.CREATED]

    def test_in_progress_can_go_to_pr_review(self):
        assert StoryStatus.PR_REVIEW in VALID_TRANSITIONS[StoryStatus.IN_PROGRESS]

    def test_pr_review_can_deploy(self):
        assert StoryStatus.DEPLOYING in VALID_TRANSITIONS[StoryStatus.PR_REVIEW]

    def test_pr_review_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.PR_REVIEW]

    def test_pr_review_can_return_to_in_progress(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.PR_REVIEW]

    def test_deploying_can_go_to_testing(self):
        assert StoryStatus.TESTING in VALID_TRANSITIONS[StoryStatus.DEPLOYING]

    def test_testing_can_complete(self):
        assert StoryStatus.COMPLETED in VALID_TRANSITIONS[StoryStatus.TESTING]

    def test_testing_can_return_to_in_progress(self):
        assert StoryStatus.IN_PROGRESS in VALID_TRANSITIONS[StoryStatus.TESTING]

    def test_testing_can_fail(self):
        assert StoryStatus.FAILED in VALID_TRANSITIONS[StoryStatus.TESTING]

    def test_waiting_user_secret_status_value(self):
        assert StoryStatus.WAITING_USER_SECRET == "waiting_user_secret"  # noqa: S105

    def test_deploying_can_wait_for_user_secret(self):
        assert StoryStatus.WAITING_USER_SECRET in VALID_TRANSITIONS[StoryStatus.DEPLOYING]

    def test_waiting_user_secret_transitions(self):
        allowed = VALID_TRANSITIONS[StoryStatus.WAITING_USER_SECRET]
        assert StoryStatus.DEPLOYING in allowed  # secret arrived → redeploy
        assert StoryStatus.FAILED in allowed  # given up manually
        assert StoryStatus.COMPLETED not in allowed  # can't skip to completed

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
