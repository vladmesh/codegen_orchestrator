"""Safe exhaustion readback; persistence and Redis admission are tested in CI."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.story_failure import story_failure_owner_text
from shared.contracts.dto.users_grant import (
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentRetryCommand,
)
from shared.models import Project, Run, Story, User, UsersGrantIntent
from src.routers.projects import access
from src.schemas.story import StoryStopTransition


@pytest.fixture(autouse=True)
def positive_deploy_policy(monkeypatch):
    monkeypatch.setattr(access, "_deploy_retry_ceiling", AsyncMock(return_value=2))


@pytest.mark.asyncio
async def test_exhausted_initial_owner_result_names_the_fenced_deliberate_action(monkeypatch):
    project, intent, run, story = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    intent.channel = "telegram"
    intent.external_id = "84"
    intent.initiating_actor = "deploy_lifecycle"
    intent.created_at = datetime.now(UTC)
    story.status = "deploying"
    monkeypatch.setattr(access, "_current_source_story", AsyncMock(return_value=(run, story)))
    db = MagicMock(commit=AsyncMock())
    await access._stop_exhausted_initial_owner(
        db, project, intent, story.id, intent.target_sha, "b" * 40
    )
    result = await access._dispatch_lifecycle(
        db, MagicMock(), project, intent, None, GrantIntentLifecycleDisposition.EXHAUSTED, False
    )
    reason = result.model_dump(mode="json")["exhaustion"]
    assert reason["code"] == "initial_owner_deployment_exhausted"
    assert reason["attempts"] == 1
    assert reason["target"]["sha"] == intent.target_sha
    assert reason["retry_command"] == {"expected_execution_run_id": intent.execution_run_id}
    assert reason["action"] == "retry_initial_owner_deployment"
    assert result.execution_run_id is None  # historical evidence is not a new dispatch
    readback = await access._as_dto(db, project, intent)
    assert readback.model_dump(mode="json")["exhaustion"] == reason


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


@pytest.mark.asyncio
async def test_zero_admission_exhaustion_offers_no_retry_action_or_owner_promise():
    project, intent, _, _ = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    intent.attempts = 0
    intent.execution_run_id = None
    exhaustion = await access._exhaustion(MagicMock(), project, intent)
    assert exhaustion is not None
    assert exhaustion.action is None and exhaustion.retry_command is None
    failure = access._exhaustion_failure(intent, exhaustion)
    assert "retry_initial_owner_deployment" not in failure.detail
    assert "retry_initial_owner_deployment" not in story_failure_owner_text(failure)
    assert "can deliberately retry" not in story_failure_owner_text(failure)


@pytest.mark.asyncio
async def test_exhausted_action_requires_the_matching_story_stop_after_landing(monkeypatch):
    project, intent, run, story = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    story.status = "deploying"
    monkeypatch.setattr(access, "_current_source_story", AsyncMock(return_value=(run, story)))
    db = MagicMock()
    assert (await access._exhaustion(db, project, intent)).action is None
    actionable = await access._exhaustion(db, project, intent, for_stop=True)
    assert actionable.retry_command.expected_execution_run_id == run.id
    access._record_story_failure(
        story, access._exhaustion_failure(intent, actionable), access.StoryStatus.FAILED
    )
    access._do_transition(story, access.StoryStatus.FAILED)
    landed = await access._exhaustion(db, project, intent)
    assert landed.action == "retry_initial_owner_deployment"
    story.quarantine_reason = {"reason": "unrelated quarantine"}
    assert (await access._exhaustion(db, project, intent)).action is None


@pytest.mark.asyncio
async def test_current_policy_controls_readback_without_changing_the_terminal_notice(monkeypatch):
    project, intent, run, story = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    story.status = "deploying"
    monkeypatch.setattr(access, "_current_source_story", AsyncMock(return_value=(run, story)))
    ceiling = AsyncMock(return_value=2)
    monkeypatch.setattr(access, "_deploy_retry_ceiling", ceiling)
    db = MagicMock()
    admitted = await access._exhaustion(db, project, intent, for_stop=True)
    access._record_story_failure(
        story, access._exhaustion_failure(intent, admitted), access.StoryStatus.FAILED
    )
    access._do_transition(story, access.StoryStatus.FAILED)
    original_reason = story.quarantine_reason.copy()
    original_notice = story.owner_notification.copy()
    assert admitted.retry_command.expected_execution_run_id == run.id
    assert "retry_initial_owner_deployment" not in original_reason["detail"]
    assert "retry_initial_owner_deployment" not in original_notice["text"]

    ceiling.return_value = 0
    unavailable = await access._exhaustion(db, project, intent)
    assert unavailable.action is None and unavailable.retry_command is None
    assert story.quarantine_reason == original_reason
    assert story.owner_notification == original_notice

    ceiling.return_value = 2
    restored = await access._exhaustion(db, project, intent)
    assert restored.retry_command.expected_execution_run_id == run.id


@pytest.mark.asyncio
async def test_zero_admission_stays_exhausted_when_policy_rises(monkeypatch):
    project, intent, _, story = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
    intent.execution_run_id = None
    intent.attempts = 0
    db = MagicMock()
    row = MagicMock()
    row.scalar_one_or_none.return_value = intent
    db.execute = AsyncMock(return_value=row)
    db.get = AsyncMock(return_value=None)
    monkeypatch.setattr(access, "_deploy_retry_ceiling", AsyncMock(return_value=2))
    result = await access._lifecycle(
        db,
        project,
        target_user=User(id=7, telegram_id=84),
        kind=GrantIntentKind.INITIAL_OWNER,
        actor="deploy_lifecycle",
        target=(None, None, intent.target_sha),
        deployed_commit_sha="b" * 40,
        story_id=story.id,
    )
    assert result[1] is None
    assert result[3] is GrantIntentLifecycleDisposition.EXHAUSTED
    assert intent.execution_run_id is None and intent.attempts == 0


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


@pytest.mark.parametrize(
    "result,accepted",
    [
        ({"deploy_outcome": "cancelled"}, True),
        (None, False),
        ({"deploy_outcome": "retry"}, False),
        ({"deploy_outcome": "cancelled", "invented": True}, False),
    ],
)
def test_cancelled_source_requires_typed_terminal_deploy_result(result, accepted):
    project, intent, run, story = _evidence()
    run.status = "cancelled"
    run.result = result
    assert access._source_matches(project, intent, run, story) is accepted


@pytest.mark.asyncio
async def test_zero_admissions_stop_only_the_current_trusted_merged_story(monkeypatch):
    project, intent, _, story = _evidence()
    intent.status = "publish_owed"
    intent.execution_run_id = None
    intent.attempts = 0
    story.status = "pr_review"
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[story, None])
    row = MagicMock()
    row.scalar_one_or_none.return_value = intent
    db.execute = AsyncMock(return_value=row)
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=None)
    monkeypatch.setattr(access, "_deploy_retry_ceiling", AsyncMock(return_value=0))
    trusted = AsyncMock()
    monkeypatch.setattr(access, "_require_current_merged_target", trusted)
    actual, run, _, disposition = await access._lifecycle(
        db,
        project,
        target_user=User(id=7, telegram_id=84),
        kind=GrantIntentKind.INITIAL_OWNER,
        actor="deploy_lifecycle",
        target=(None, None, intent.target_sha),
        deployed_commit_sha="b" * 40,
        story_id=story.id,
        merged_pr_number=story.pr_number,
    )
    assert run is None and disposition is GrantIntentLifecycleDisposition.EXHAUSTED
    assert actual.execution_run_id is None and actual.attempts == 0
    assert story.status == "failed"
    assert story.quarantine_reason["code"] == "initial_owner_deployment_exhausted"
    assert story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
    trusted.assert_awaited_once()


@pytest.mark.asyncio
async def test_premature_human_retry_refuses_before_reset_or_run(monkeypatch):
    project, intent, _, _ = _evidence()
    intent.status = "retryable"
    intent.attempts = 1
    db = MagicMock()
    row = MagicMock()
    row.scalar_one_or_none.return_value = intent
    db.execute = AsyncMock(return_value=row)
    db.flush = AsyncMock()
    monkeypatch.setattr(access, "_execution_is_live", AsyncMock(return_value=None))
    monkeypatch.setattr(access, "_deploy_retry_ceiling", AsyncMock(return_value=3))
    with pytest.raises(HTTPException) as exc:
        await access._lifecycle(
            db,
            project,
            target_user=User(id=7, telegram_id=84),
            kind=GrantIntentKind.INITIAL_OWNER,
            actor="user:7",
            target=(None, None, intent.target_sha),
            deployed_commit_sha="b" * 40,
            story_id="native-story",
            retry_command=GrantIntentRetryCommand(expected_execution_run_id="native-run"),
        )
    assert exc.value.status_code == 409
    assert intent.status == "retryable" and intent.attempts == 1
    assert intent.retry_history == []
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhaustion_stops_once_and_owes_both_audiences(monkeypatch):
    project, intent, run, story = _evidence()
    intent.status = "failed"
    intent.detail = "deployment retry ceiling exhausted"
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


def _repaired_head_approval(*, pr_number=42, approved="d" * 40):
    return {
        "actor": "user:7",
        "approved_at": datetime.now(UTC).isoformat(),
        "pr_number": pr_number,
        "head_sha": "a" * 40,
        "merge_commit_sha": "b" * 40,
        "approved_commit_sha": approved,
        "superseded_commit_sha": "b" * 40,
        "quarantine_reason": {"deploy_outcome": "images_not_published"},
    }


def test_source_binds_the_approved_repaired_head_of_exactly_that_merge():
    """After an approved repaired head, that commit is what the merged story built."""
    project, intent, run, story = _evidence()
    run.run_metadata["deployed_commit_sha"] = "d" * 40
    assert not access._source_matches(project, intent, run, story)

    story.generated_product_timeline["repaired_head_deploy_approval"] = _repaired_head_approval()
    assert access._source_matches(project, intent, run, story)
    run.run_metadata["deployed_commit_sha"] = "b" * 40
    assert not access._source_matches(project, intent, run, story)

    story.generated_product_timeline["repaired_head_deploy_approval"] = _repaired_head_approval(
        pr_number=41
    )
    assert access._source_matches(project, intent, run, story)
    story.generated_product_timeline["repaired_head_deploy_approval"] = {"actor": "forged"}
    assert not access._source_matches(project, intent, run, story)
