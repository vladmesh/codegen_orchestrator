"""A queued owner grant's owed publication is discovered by native supervision."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.users_grant import (
    GrantIntent,
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
    GrantIntentStatus,
)
from src.tasks.supervisor import deploy


@pytest.mark.asyncio
async def test_deploying_sweeps_recover_the_same_committed_grant_run_once(monkeypatch):
    project_id = uuid.uuid4()
    story = SimpleNamespace(id="retry-story", project_id=project_id)
    intent_id = f"users-grant-initial_owner-{project_id.hex}-84"
    run = SimpleNamespace(
        id="deploy-grant-committed-retry",
        status=RunStatus.QUEUED,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        result=None,
        run_metadata={
            "users_grant_intent": intent_id,
            "head_sha": "a" * 40,
            "deployed_commit_sha": "e" * 40,
            "grant_epoch": 1,
            "grant_attempt": 1,
            "triggered_by": "users_grant_intent",
            "deploy_action": "create",
        },
    )
    intent = GrantIntent(
        id=intent_id,
        kind=GrantIntentKind.INITIAL_OWNER,
        project_id=str(project_id),
        channel="telegram",
        external_id="84",
        target_sha="a" * 40,
        initiating_actor="deploy_lifecycle",
        status=GrantIntentStatus.PUBLISH_OWED,
        attempts=1,
        execution_run_id=run.id,
    )
    api = AsyncMock()
    api.get_stories_by_status.return_value = [story]
    api.get_latest_run_by_story.return_value = run
    api.get_users_grant_intent.side_effect = [
        intent,
        intent.model_copy(update={"status": GrantIntentStatus.QUEUED}),
    ]
    api.resume_initial_owner_grant.return_value = GrantIntentLifecycleResult(
        intent_id=intent_id,
        status=GrantIntentStatus.QUEUED,
        disposition=GrantIntentLifecycleDisposition.IN_FLIGHT,
    )
    monkeypatch.setattr(deploy, "_qa_handoff_recovery_minutes", lambda: 1)

    first = await deploy.supervise_deploying_stories(api, MagicMock())
    second = await deploy.supervise_deploying_stories(api, MagicMock())
    assert first["retried"] == second["retried"] == 0  # no fresh Run claimed
    api.resume_initial_owner_grant.assert_awaited_once_with(
        str(project_id),
        story_id=story.id,
        head_sha="a" * 40,
        deployed_commit_sha="e" * 40,
        expected_execution_run_id=run.id,
    )
    assert api.get_users_grant_intent.await_count == 2
    api.create_run.assert_not_awaited()
