"""A verified handoff retires projections, while different controls survive."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from shared.contracts.dto.commit_publication import CommitPublication, PublicationFailure
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.models import Story
from src.routers import commit_recovery
from src.routers._story_helpers import _record_story_failure


def episode():
    evidence = CommitPublication(
        attempt_id="eng-original",
        worker_id="worker-original",
        commit_sha="b" * 40,
        branch="story/story-fixture",
        failure=PublicationFailure.PUSH_REFUSED,
    ).model_dump(mode="json")
    story = Story(
        id="story-fixture",
        project_id="00000000-0000-0000-0000-000000000001",
        status="waiting_human_review",
        waiting_on="human",
        quarantine_reason={
            "reason": "story_failure",
            "commit_publication": evidence,
            "failure": {"commit_publication": evidence},
        },
    )
    project = SimpleNamespace(config={"commit_publication": evidence, "workspace_ready": True})
    run = SimpleNamespace(
        id="eng-original",
        run_metadata={"commit_publication": evidence, "worker_id": "worker-original"},
    )
    claim = SimpleNamespace(
        attempt_id=run.id,
        story_id=story.id,
        commit_sha="b" * 40,
        actor="internal_service",
        stop_id=None,
        claimed_at=datetime.now(UTC),
        handed_off_at=None,
        receipt=CommitPublication(
            published=True,
            commit_sha="b" * 40,
            remote_sha="b" * 40,
            attempt_id=run.id,
            worker_id="worker-original",
            branch="story/story-fixture",
        ).model_dump(mode="json"),
        identity={
            "worker_id": "worker-original",
            "attempt_id": run.id,
            "story_id": story.id,
            "project_id": str(story.project_id),
            "initiating_run_id": "init-fixture",
            "repository_id": "repo-fixture",
            "repository_url": "https://github.com/fixture/owned.git",
            "branch": "story/story-fixture",
            "baseline": "a" * 40,
            "cycle": None,
            "iteration": None,
        },
    )
    db = MagicMock(commit=AsyncMock(), get=AsyncMock(return_value=claim))
    return story, project, run, claim, db


async def test_handoff_retires_every_story_projection_before_a_later_failure():
    story, project, run, claim, db = episode()
    original = dict(run.run_metadata)
    await commit_recovery._handoff_recovery(
        story, None, project, run, claim, "internal_service", db
    )
    assert "commit_publication" not in str(story.quarantine_reason)
    assert project.config == {"workspace_ready": True}
    assert run.run_metadata == original and claim.handed_off_at
    later = StoryFailure(
        code=StoryFailureCode.NO_NEW_COMMIT, source="engineering", detail="Later ordinary attempt"
    )
    _record_story_failure(story, later, StoryStatus.WAITING_HUMAN_REVIEW)
    assert story.quarantine_reason == later.model_dump(mode="json")


@pytest.mark.parametrize("projection", ["project", "story", "nested_story", "task"])
async def test_different_publication_hold_is_not_retired(projection):
    story, project, run, claim, db = episode()
    held = dict(project.config["commit_publication"], attempt_id="eng-newer")
    task = None
    if projection == "project":
        project.config["commit_publication"] = held
    elif projection == "story":
        story.quarantine_reason["commit_publication"] = held
    elif projection == "nested_story":
        story.quarantine_reason["failure"]["commit_publication"] = held
    else:
        task = SimpleNamespace(status="done", failure_metadata={"commit_publication": held})
    before = repr((story.quarantine_reason, project.config))
    with pytest.raises(HTTPException):
        await commit_recovery._handoff_recovery(
            story, task, project, run, claim, "internal_service", db
        )
    assert repr((story.quarantine_reason, project.config)) == before
    assert claim.handed_off_at is None
    db.commit.assert_not_awaited()


async def test_late_internal_park_cannot_resurrect_handed_off_attempt():
    from src.publication_park import park_publication

    story, project, run, claim, db = episode()
    evidence = CommitPublication.model_validate(project.config["commit_publication"])
    claim.handed_off_at = datetime.now(UTC)
    story.status = "in_progress"
    story.quarantine_reason = None
    project.config = {"workspace_ready": True}
    run.project_id = story.project_id
    db.scalar = AsyncMock(return_value=project)
    await park_publication(story, None, run, evidence, db)
    assert story.status == "in_progress" and story.quarantine_reason is None
    assert project.config == {"workspace_ready": True}


async def test_quarantine_authority_uses_original_attempt_and_handoff_claim(monkeypatch):
    from src import attempt_disposition
    from src.publication_park import guard_publication_failure, guard_quarantine_patch

    story, _, run, _, db = episode()
    story.quarantine_reason = {"reason": "ordinary cause"}
    monkeypatch.setattr(attempt_disposition, "recovered_attempts", AsyncMock(return_value=set()))
    with pytest.raises(HTTPException):
        await guard_quarantine_patch(story, None, [run], db)
    failure = StoryFailure(
        code=StoryFailureCode.WORKER_COMMIT_NOT_PUBLISHED,
        source="engineering",
        detail="Late original refusal",
        commit_publication=CommitPublication.model_validate(run.run_metadata["commit_publication"]),
    )
    await guard_publication_failure(failure, [run], db)
    monkeypatch.setattr(attempt_disposition, "recovered_attempts", AsyncMock(return_value={run.id}))
    await guard_quarantine_patch(story, None, [run], db)
    with pytest.raises(HTTPException):
        await guard_publication_failure(failure, [run], db)
    with pytest.raises(HTTPException):
        await guard_quarantine_patch(story, {"failure": failure.model_dump(mode="json")}, [run], db)


async def test_adopted_unverified_diagnostic_retires_only_after_exact_proof():
    story, project, run, claim, db = episode()
    for publication in (
        story.quarantine_reason["commit_publication"],
        story.quarantine_reason["failure"]["commit_publication"],
        project.config["commit_publication"],
    ):
        publication["commit_sha"] = None
        publication["failure"] = PublicationFailure.OBJECT_MISSING.value
    # The controller already validated deliberate adoption and native proof.
    # An absent diagnostic SHA is not substituted into the original paid Run.
    await commit_recovery._handoff_recovery(
        story, None, project, run, claim, "internal_service", db
    )
    assert "commit_publication" not in str(story.quarantine_reason)
