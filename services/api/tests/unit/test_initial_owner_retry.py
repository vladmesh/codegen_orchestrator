"""Safe exhaustion readback; persistence and Redis admission are tested in CI."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from shared.contracts.dto.users_grant import GrantIntentLifecycleDisposition
from shared.models import Project, Run, Story, User, UsersGrantIntent
from src.routers.projects import access
from src.schemas.story import StoryStopTransition


@pytest.mark.asyncio
async def test_exhausted_initial_owner_result_names_the_fenced_deliberate_action():
    project = Project(id=uuid.uuid4(), owner_id=7)
    intent = UsersGrantIntent(
        id=f"users-grant-initial_owner-{project.id.hex}-84",
        project_id=project.id,
        kind="initial_owner",
        channel="telegram",
        external_id="84",
        initiating_actor="deploy_lifecycle",
        status="failed",
        detail="deployment retry ceiling exhausted",
        attempts=3,
        execution_run_id="deploy-grant-last-real-attempt",
        target_sha="a" * 40,
        created_at=datetime.now(UTC),
    )
    db = MagicMock(commit=AsyncMock())
    result = await access._dispatch_lifecycle(
        db, MagicMock(), project, intent, None, GrantIntentLifecycleDisposition.EXHAUSTED, False
    )
    reason = result.model_dump(mode="json")["exhaustion"]
    assert reason["code"] == "initial_owner_deployment_exhausted"
    assert reason["attempts"] == 3
    assert reason["target"]["sha"] == intent.target_sha
    assert reason["retry_command"] == {"expected_execution_run_id": intent.execution_run_id}
    assert reason["action"] == "retry_initial_owner_deployment"
    assert result.execution_run_id is None  # historical evidence is not a new dispatch
    assert access._as_dto(intent).model_dump(mode="json")["exhaustion"] == reason


def _evidence():
    cycle = datetime.now(UTC) - timedelta(minutes=3)
    project = Project(id=uuid.uuid4(), owner_id=7)
    intent = UsersGrantIntent(
        id="native-intent",
        project_id=project.id,
        kind="initial_owner",
        target_sha="a" * 40,
        attempts=1,
        execution_run_id="native-run",
        retry_history=[],
        target_history=[],
    )
    run = Run(
        id="native-run",
        type="deploy",
        project_id=project.id,
        user_id=7,
        story_id="native-story",
        status="failed",
        created_at=cycle + timedelta(minutes=2),
        run_metadata={
            "head_sha": "a" * 40,
            "deployed_commit_sha": "b" * 40,
            "users_grant_intent": intent.id,
            "grant_epoch": 0,
            "grant_attempt": 1,
        },
    )
    story = Story(
        id="native-story",
        project_id=project.id,
        created_at=cycle,
        pr_number=42,
        generated_product_timeline={
            "pull_request": {
                "number": 42,
                "state": "closed",
                "head_sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
                "merged_at": (cycle + timedelta(minutes=1)).isoformat(),
            }
        },
    )
    return project, intent, run, story


@pytest.mark.parametrize(
    "changed", ["cycle", "pr", "head", "built", "owner", "intent", "live", "missing"]
)
def test_retry_source_refuses_changed_or_malformed_native_evidence(changed):
    project, intent, run, story = _evidence()
    assert access._source_matches(project, intent, run, story)
    if changed == "cycle":
        story.reopened_at = datetime.now(UTC)
    elif changed == "pr":
        story.pr_number = 43
    elif changed == "head":
        intent.target_sha = "c" * 40
    elif changed == "built":
        run.run_metadata["deployed_commit_sha"] = "c" * 40
    elif changed == "owner":
        project.owner_id = 8
    elif changed == "intent":
        intent.execution_run_id = "another-run"
    elif changed == "live":
        run.status = "queued"
    else:
        run.run_metadata.pop("deployed_commit_sha")
    assert not access._source_matches(project, intent, run, story)


def test_epoch_requires_real_admissions_and_the_exact_exhausted_counter():
    _, intent, run, _ = _evidence()
    assert access._epoch_matches(intent, run, [run])
    assert not access._epoch_matches(intent, run, [])
    intent.attempts = 3
    assert not access._epoch_matches(intent, run, [run])
    intent.attempts = 1
    intent.retry_history = [{"expected_execution_run_id": "prior-exhausted-run"}]
    assert not access._epoch_matches(intent, run, [run])


def test_released_source_still_requires_native_admissions():
    project, intent, run, story = _evidence()
    run.run_metadata.pop("grant_epoch")
    run.run_metadata.pop("grant_attempt")
    assert access._source_matches(project, intent, run, story)
    assert access._epoch_matches(intent, run, [run])
    assert not access._epoch_matches(intent, run, [])


@pytest.mark.asyncio
async def test_exhaustion_stops_once_and_owes_both_audiences(monkeypatch):
    project, intent, run, story = _evidence()
    story.status = "deploying"
    monkeypatch.setattr(access, "_current_source_story", AsyncMock(return_value=(run, story)))
    await access._stop_exhausted_initial_owner(
        MagicMock(), project, intent, story.id, intent.target_sha, "b" * 40
    )
    assert story.status == "failed"
    assert story.quarantine_reason["code"] == "initial_owner_deployment_exhausted"
    assert story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
    notice = story.owner_notification
    await access._stop_exhausted_initial_owner(
        MagicMock(), project, intent, story.id, intent.target_sha, "b" * 40
    )
    assert story.owner_notification == notice


@pytest.mark.asyncio
async def test_stale_exhaustion_callback_leaves_current_story_alone(monkeypatch):
    project, intent, run, story = _evidence()
    story.status = "deploying"
    monkeypatch.setattr(access, "_current_source_story", AsyncMock(return_value=(run, story)))
    await access._stop_exhausted_initial_owner(
        MagicMock(), project, intent, story.id, "c" * 40, "b" * 40
    )
    assert story.status == "deploying"
    assert story.quarantine_reason is None and story.owner_notification is None


def test_generic_stop_body_cannot_forge_native_grant_exhaustion():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="grant lifecycle"):
        StoryStopTransition.model_validate(
            {
                "failure": {
                    "code": "initial_owner_deployment_exhausted",
                    "source": "api",
                    "detail": "forged native stop",
                }
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["add_user", "incoming_owner"])
async def test_other_kinds_retain_their_existing_policy_increase_behavior(monkeypatch, kind):
    from shared.contracts.dto.users_grant import GrantIntentKind

    project, intent, _, _ = _evidence()
    intent.kind = kind
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    intent.attempts = 3
    db = MagicMock()
    row = MagicMock()
    row.scalar_one_or_none.return_value = intent
    db.execute = AsyncMock(return_value=row)
    db.flush = AsyncMock()
    monkeypatch.setattr(access, "_execution_is_live", AsyncMock(return_value=None))
    monkeypatch.setattr(access, "_deploy_retry_ceiling", AsyncMock(return_value=4))
    _, run, _, disposition = await access._lifecycle(
        db,
        project,
        target_user=User(id=7, telegram_id=84),
        kind=GrantIntentKind(kind),
        actor="user:7",
        target=(None, None, intent.target_sha),
        deployed_commit_sha="b" * 40,
        story_id=None,
        explicit_user_retry=True,
    )
    assert disposition is GrantIntentLifecycleDisposition.DISPATCHED
    assert run is not None and intent.attempts == 4 and intent.retry_history == []
