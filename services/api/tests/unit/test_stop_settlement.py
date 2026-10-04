"""A known empty outcome retains its terminal writer across a Story stop."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.commit_publication import EngineeringStop
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.dto.story import StoryAcceptance, StoryStatus
from shared.models import Story
from src.routers._story_helpers import _land_on, _release_engineering_stop
from src.routers.commit_recovery import _finish_stopped_run
from src.routers.stories import _complete_story


@pytest.mark.asyncio
async def test_stop_does_not_cancel_a_retained_empty_result_before_its_terminal_writer():
    result = EngineeringRunResult(
        engineering_status=EngineeringStatus.FAILED,
        failure_reason=EngineeringFailureReason.NO_NEW_COMMIT,
        worker_report="The worker completed its paid turn without changes.",
    ).model_dump(mode="json")
    run = SimpleNamespace(
        id="eng-empty", status="running", run_metadata={}, result=result, completed_at=None
    )
    with (
        patch("shared.commit_publication.pending_publication", AsyncMock(return_value=None)),
        patch("src.routers.runs._settle_terminal_accounting", AsyncMock()),
    ):
        await _finish_stopped_run(run, AsyncMock(), AsyncMock(), SimpleNamespace(), [])
    assert run.status == "running"
    assert run.result == result


@pytest.mark.asyncio
async def test_explicit_acceptance_releases_its_stop_before_the_completion_transition():
    now = datetime.now(UTC)
    stop = EngineeringStop(id="stop-current", actor="internal_service", stopped_at=now)
    story = Story(
        id="story-accepted",
        project_id=uuid.uuid4(),
        title="Accepted result",
        type="product",
        status="waiting_human_review",
        waiting_on="human_review",
        priority=0,
        created_by="po",
        unverified_decisions=[],
        created_at=now,
        updated_at=now,
        engineering_stop=stop.model_dump(mode="json"),
    )
    acceptance = StoryAcceptance(
        actor="admin-console:operator",
        basis="Reviewed result",
        accepted_at=now,
    )
    db = AsyncMock()
    with patch("src.routers.stories._owe_completed_story_notification", AsyncMock()):
        result = await _complete_story(story, db, acceptance=acceptance)
    assert result.status.value == "completed"
    released = EngineeringStop.model_validate(story.engineering_stop)
    assert released.id == stop.id and released.release_actor == acceptance.actor
    assert released.released_at is not None
    db.commit.assert_awaited_once()


def test_native_recovery_requires_the_exact_stop_and_uses_authenticated_actor():
    stop = EngineeringStop(
        id="stop-current", actor="internal_service", stopped_at=datetime.now(UTC)
    )
    story = SimpleNamespace(id="story-stopped", engineering_stop=stop.model_dump(mode="json"))
    db = MagicMock()
    for selected in (None, "stop-older"):
        with pytest.raises(HTTPException) as refused:
            _release_engineering_stop(story, selected, "admin:42", db)
        assert refused.value.status_code == 409
        assert story.engineering_stop == stop.model_dump(mode="json")
        db.add.assert_not_called()
    _release_engineering_stop(story, stop.id, "admin:42", db)
    _land_on(story, StoryStatus.IN_PROGRESS)
    assert story.status == "in_progress" and story.waiting_on == "none"
    audit = db.add.call_args.args[0]
    assert audit.actor == "admin:42" and audit.outcome == "released"
    assert EngineeringStop.model_validate(story.engineering_stop).release_actor == audit.actor


def test_a_stale_terminal_writer_cannot_replace_retained_execution_facts():
    from shared.contracts.dto.run import EMPTY_RESULT_TERMINAL_KEY, EmptyEngineeringTerminal
    from src.routers.runs import _require_retained_empty_outcome, _validate_empty_retention

    retained = EmptyEngineeringTerminal.model_validate(
        {
            "status": "failed",
            "error_message": "No new commit",
            "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
            "engineering_attempt": {"provider": "openai", "input_tokens": 17},
        }
    )
    run = SimpleNamespace(
        type="engineering",
        task_id=None,
        run_metadata={EMPTY_RESULT_TERMINAL_KEY: retained.model_dump(mode="json")},
    )
    for replacement in (
        {"status": "cancelled"},
        {"status": "failed", "result": {"engineering_status": "failed"}},
        {"error_message": "Worker disappeared"},
    ):
        with pytest.raises(HTTPException) as refused:
            _require_retained_empty_outcome(run, replacement, None)
        assert refused.value.status_code == 409
    with pytest.raises(HTTPException):
        _validate_empty_retention(run, None, {})
    terminal = retained.model_dump(mode="json")
    _require_retained_empty_outcome(run, terminal, retained.engineering_attempt)


@pytest.mark.asyncio
async def test_same_typed_stop_replay_preserves_its_cause_and_rejects_another():
    from shared.contracts.dto.story_failure import StoryFailure
    from src.routers.stories import human_review_story
    from src.schemas.story import StoryStopTransition

    now = datetime.now(UTC)
    cause = StoryFailure(code="no_new_commit", source="engineering", detail="Attempt eng-1: empty")
    stop = EngineeringStop(id="stop-current", actor="internal_service", stopped_at=now)
    story = Story(
        id="story-stopped",
        project_id=uuid.uuid4(),
        title="Stopped",
        type="product",
        status="waiting_human_review",
        waiting_on="human_review",
        priority=0,
        created_by="po",
        unverified_decisions=[],
        created_at=now,
        updated_at=now,
        engineering_stop=stop.model_dump(mode="json"),
        quarantine_reason=cause.model_dump(mode="json"),
    )
    before = dict(story.quarantine_reason)
    with (
        patch(
            "src.attempt_disposition.lock_story_attempts",
            AsyncMock(return_value=(story, [], None, [])),
        ),
        patch("src.routers.commit_recovery.reconcile_stop", AsyncMock()),
        patch("src.dependencies.get_redis_client", MagicMock()),
    ):
        await human_review_story(
            story.id, StoryStopTransition(failure=cause), AsyncMock(), "internal_service"
        )
        assert story.quarantine_reason == before
        assert story.engineering_stop == stop.model_dump(mode="json")
        different = cause.model_copy(update={"detail": "Another attempt"})
        with pytest.raises(HTTPException) as refused:
            await human_review_story(
                story.id, StoryStopTransition(failure=different), AsyncMock(), "internal_service"
            )
        assert refused.value.status_code == 409
        assert story.quarantine_reason == before
