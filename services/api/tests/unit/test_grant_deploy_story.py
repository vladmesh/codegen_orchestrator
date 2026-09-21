"""The grant deploy carries the story its Run was created for.

For a freshly registered project the `initial_owner` grant deploy is the only
deploy the product gets. Its Run is created with the story, and the consumer
reads the confirmed brief's settings through that story; a message that drops
it leaves the product unseeded. A temporary live-target grant has no story and
its message says so with the DTO's own empty value.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from shared.contracts.dto.users_grant import (
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.models import Project, Run, UsersGrantIntent
from src.routers._recipients import ProjectRecipient
from src.routers.projects import access

PROJECT_ID = uuid.UUID("202a5f2b-3e87-4f32-bbde-84d31a004e14")
HEAD_SHA = "a" * 40
BUILT_SHA = "b" * 40


def _project() -> Project:
    return Project(id=PROJECT_ID, owner_id=1, config={"modules": ["tg_bot"]})


def _intent(kind: GrantIntentKind) -> UsersGrantIntent:
    return UsersGrantIntent(
        id=f"grant-{kind.value}",
        kind=kind.value,
        project_id=PROJECT_ID,
        channel="telegram",
        external_id="8202532144",
        target_sha=HEAD_SHA,
        status=GrantIntentStatus.PUBLISH_OWED.value,
        attempts=1,
    )


def _run(story_id: str | None) -> Run:
    return Run(
        id="deploy-grant-cec48072",
        type="deploy",
        project_id=PROJECT_ID,
        user_id=1,
        story_id=story_id,
        status="queued",
        run_metadata={
            "head_sha": HEAD_SHA,
            "deployed_commit_sha": BUILT_SHA,
            "triggered_by": "users_grant_intent",
            "deploy_action": DeployAction.CREATE.value,
        },
    )


async def _published(dispatch) -> DeployMessage:
    redis = MagicMock()
    redis.publish_message = AsyncMock()
    db = MagicMock()
    db.commit = AsyncMock()
    with patch.object(
        access,
        "resolve_project_recipient",
        new=AsyncMock(return_value=ProjectRecipient(telegram_chat_id="123")),
    ):
        await dispatch(db, redis)
    redis.publish_message.assert_awaited_once()
    message = redis.publish_message.await_args.args[1]
    assert isinstance(message, DeployMessage)
    return message


@pytest.mark.asyncio
async def test_initial_owner_grant_deploy_carries_the_runs_story():
    intent = _intent(GrantIntentKind.INITIAL_OWNER)
    run = _run("story-c3866cf1")

    async def dispatch(db, redis):
        await access._dispatch_lifecycle(
            db,
            redis,
            _project(),
            intent,
            run,
            GrantIntentLifecycleDisposition.DISPATCHED,
            True,
        )

    message = await _published(dispatch)

    assert message.story_id == "story-c3866cf1"
    assert message.triggered_by is DeployTrigger.PO
    assert message.action is DeployAction.CREATE


@pytest.mark.asyncio
async def test_live_target_grant_deploy_stays_storyless():
    intent = _intent(GrantIntentKind.ADD_USER)
    seen: dict = {}

    async def lifecycle(db, project, **kwargs):
        seen["story_id"] = kwargs["story_id"]
        return intent, _run(kwargs["story_id"]), True, GrantIntentLifecycleDisposition.DISPATCHED

    async def dispatch(db, redis):
        with (
            patch.object(access, "_live_target", new=AsyncMock(return_value=(42, 7, HEAD_SHA))),
            patch.object(
                access, "_deployed_commit_for_deployment", new=AsyncMock(return_value=BUILT_SHA)
            ),
            patch.object(access, "_lifecycle", new=lifecycle),
        ):
            await access._stage_live_intent(
                db, redis, _project(), MagicMock(), GrantIntentKind.ADD_USER, "owner"
            )

    message = await _published(dispatch)

    assert seen["story_id"] is None
    assert message.story_id == ""
