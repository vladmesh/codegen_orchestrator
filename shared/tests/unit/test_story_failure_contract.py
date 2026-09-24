"""The typed reason a platform failure leaves on a story, and what may be read from it."""

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from shared.contracts.dto.story_failure import (
    STORY_FAILURE_DETAIL_LIMIT,
    STORY_FAILURE_REASON,
    StoryFailure,
    StoryFailureCode,
    in_work_cycle,
    story_failure_admin_text,
    story_failure_owner_text,
)

TOKEN = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # noqa: S105 - a fake installation token


def test_the_detail_is_redacted_before_it_can_be_stored():
    failure = StoryFailure(
        code=StoryFailureCode.SCAFFOLD_FAILED,
        source="scaffolder",
        detail=f"fatal: could not read https://x-access-token:{TOKEN}@github.com/o/r.git",
    )

    assert TOKEN not in failure.detail
    assert "[redacted]@github.com" in failure.detail


def test_the_detail_is_bounded_so_it_is_never_a_traceback():
    failure = StoryFailure(
        code=StoryFailureCode.SCAFFOLD_TIMEOUT, source="architect", detail="x" * 5000
    )

    assert len(failure.detail) == STORY_FAILURE_DETAIL_LIMIT + len("...")


def test_the_stored_shape_names_itself_and_refuses_extra_fields():
    failure = StoryFailure(code=StoryFailureCode.SCAFFOLD_FAILED, source="s", detail="d")

    stored = failure.model_dump(mode="json")
    assert stored["reason"] == STORY_FAILURE_REASON
    assert StoryFailure.model_validate(stored) == failure
    with pytest.raises(ValidationError):
        StoryFailure.model_validate({**stored, "secret": "x"})


@pytest.mark.parametrize("code", list(StoryFailureCode))
def test_every_code_tells_the_owner_what_stopped_and_why(code):
    failure = StoryFailure(code=code, source="architect", detail="Repository not found")

    text = story_failure_owner_text(failure)

    assert "stopped" in text
    assert "Repository not found" in text
    assert "a person has to fix it" in text
    assert "Repository not found" in story_failure_admin_text("story-1", "p-1", failure)


def test_the_work_cycle_starts_at_the_reopen_but_keeps_open_tasks():
    reopened = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    before = datetime(2026, 9, 24, 11, 0, tzinfo=UTC)

    assert in_work_cycle(before, None, "done")
    assert not in_work_cycle(before, reopened, "done")
    assert not in_work_cycle(before, reopened, "cancelled")
    # The CI-retry move creates its fix task a moment before it stamps the reopen.
    assert in_work_cycle(before, reopened, "todo")
    assert in_work_cycle(datetime(2026, 9, 24, 12, 0), reopened, "done")
