"""An install story parked after its PR's CI failed is re-planned by an operator."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
import pytest
from test_install_contract import install_payload

from shared.contracts.dto.catalog_install import (
    InstallCommand,
    InstallOperation,
    InstallOperatorRequest,
)
from shared.contracts.dto.story_failure import in_work_cycle
from shared.contracts.queues.architect import ArchitectMessage
from shared.models import TaskEvent, WorkAdmissionAudit
from shared.queues import ARCHITECT_QUEUE
from src.catalog_install import install_command
from src.catalog_install_recovery import ARCHITECT_PUBLISH_FAILED, operator_install_recovery

STOP_ID = "stop-ci"
CHAT_ID = "4242"
CI_DETAIL = (
    "Catalog installation requires review: PR CI failed at "
    + "c" * 40
    + "; review https://github.com/synthetic/notes/pull/7. No automatic engineering repair."
)


class FakeGitHub:
    """The one GitHub read replan makes: the remote story branch, or its absence."""

    def __init__(self, head):
        self.head = head
        self.refs = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get_ref_sha(self, owner, repo, ref):
        self.refs.append((owner, repo, ref))
        return self.head


class World(SimpleNamespace):
    """A parked install story, its done install Task and every fake boundary."""


@pytest.fixture
def world(monkeypatch):
    now = datetime.now(UTC)
    created = now - timedelta(days=1)
    pid = uuid.uuid4()
    operation = InstallOperation(
        id="install-1",
        project_id=pid,
        task_id="task-install",
        story_id="story-1",
        repository_id="repo-1",
        cycle_started_at=created,
        state="published",
        stage="published",
        head_sha="a" * 40,
        base_sha="b" * 40,
    )
    task = SimpleNamespace(
        id=operation.task_id,
        type="install",
        project_id=pid,
        story_id=operation.story_id,
        repository_id=operation.repository_id,
        status="done",
        created_at=created,
        dispatch_admitted=True,
        blocked_by_task_id=None,
        install=install_payload(),
        install_operation=operation.model_dump(mode="json"),
    )
    cause = {"code": "scaffold_failed", "source": "scheduler", "detail": CI_DETAIL}
    story = SimpleNamespace(
        id=operation.story_id,
        project_id=pid,
        created_at=created,
        reopened_at=None,
        status="waiting_human_review",
        waiting_on="human_review",
        status_entered_at=now,
        quarantine_reason=cause,
        engineering_stop={"id": STOP_ID, "actor": "scheduler", "stopped_at": now.isoformat()},
    )
    repository = SimpleNamespace(
        project_id=pid, is_managed=True, name="notes", git_url="https://github.com/synthetic/notes"
    )
    calls = []
    github = FakeGitHub(None)
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=repository),
        add=Mock(),
        commit=AsyncMock(side_effect=lambda: calls.append("commit")),
    )
    redis = SimpleNamespace(
        publish_message=AsyncMock(side_effect=lambda *_: calls.append("publish"))
    )
    monkeypatch.setattr(
        "src.catalog_install_recovery._lock_dispatch_tasks",
        AsyncMock(return_value=(task, {}, None, story.id)),
    )
    monkeypatch.setattr(
        "src.routers._story_helpers._get_story_for_update", AsyncMock(return_value=story)
    )
    monkeypatch.setattr("src.routers.projects_guards.load_locked_project", AsyncMock())
    monkeypatch.setattr("src.catalog_install_recovery.GitHubAppClient", github)
    monkeypatch.setattr(
        "src.routers._recipients.resolve_project_chat_id", AsyncMock(return_value=CHAT_ID)
    )
    return World(
        now=now,
        operation=operation,
        task=task,
        story=story,
        cause=cause,
        github=github,
        db=db,
        redis=redis,
        calls=calls,
    )


def request(world, **overrides):
    fields = {"operation_id": world.operation.id, "action": "replan", "stop_id": STOP_ID}
    return InstallOperatorRequest(**{**fields, **overrides})


async def replan(world, **overrides):
    return await operator_install_recovery(
        world.task.id, request(world, **overrides), "user:admin", world.db, redis=world.redis
    )


def added(world, kind):
    return [call.args[0] for call in world.db.add.call_args_list if isinstance(call.args[0], kind)]


@pytest.mark.asyncio
async def test_replan_cancels_the_install_reopens_the_story_and_asks_the_architect(world):
    answer = await replan(world)

    assert answer.outcome == "settled" and answer.reason is None
    assert answer.operation == world.operation
    # The stop is released by its owner, with its audit row.
    assert world.story.engineering_stop["released_at"] is not None
    assert world.story.engineering_stop["release_actor"] == "user:admin"
    assert [audit.outcome for audit in added(world, WorkAdmissionAudit)] == ["released"]
    events = added(world, TaskEvent)
    note = [event for event in events if event.event_type == "note"]
    assert [event.details for event in note] == [
        {
            "catalog_install_settlement": world.operation.model_dump(mode="json"),
            "operator_action": "replan",
        }
    ]
    hops = [(e.from_status, e.to_status) for e in events if e.event_type == "status_change"]
    assert hops == [("done", "backlog"), ("backlog", "cancelled")]
    assert world.task.status == "cancelled"
    # The settled operation stays the record of what was published.
    assert world.task.install_operation == world.operation.model_dump(mode="json")
    assert world.story.status == "reopened" and world.story.waiting_on == "none"
    assert world.story.reopened_at is not None and world.story.reopened_at >= world.now
    assert world.story.quarantine_reason is None
    assert world.github.refs == [("synthetic", "notes", "heads/story/story-1")]
    # Published once, after the commit, built as a reopen is.
    assert world.calls == ["commit", "publish"]
    world.redis.publish_message.assert_awaited_once()
    queue, message = world.redis.publish_message.await_args.args
    assert queue == ARCHITECT_QUEUE and isinstance(message, ArchitectMessage)
    assert message.model_dump(include={"story_id", "project_id", "telegram_chat_id"}) == {
        "story_id": "story-1",
        "project_id": str(world.story.project_id),
        "telegram_chat_id": CHAT_ID,
    }
    assert message.is_reopen and message.user_report is None


@pytest.mark.asyncio
async def test_replanned_install_task_is_history_and_never_admitted_again(world, monkeypatch):
    await replan(world)

    # Not counted: a closed task from before the reopen is outside the work cycle.
    assert not in_work_cycle(world.task.created_at, world.story.reopened_at, world.task.status)
    # Not dispatched: its own admission refuses the older cycle and changes nothing.
    world.story.status = "in_progress"
    monkeypatch.setattr(
        "src.catalog_install._lock_dispatch_tasks",
        AsyncMock(return_value=(world.task, {}, None, world.story.id)),
    )
    monkeypatch.setattr(
        "src.catalog_install._take_story_roster", AsyncMock(return_value=([], None))
    )
    world.db.commit.reset_mock()
    decision = await install_command(world.task.id, InstallCommand(action="admit"), world.db)
    assert decision.outcome == "refused" and decision.reason == "stale_cycle"
    assert world.task.status == "cancelled"
    world.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lost_architect_job_is_reported_after_the_commit(world):
    world.redis.publish_message.side_effect = ConnectionError("redis down")

    answer = await replan(world)

    assert answer.outcome == "settled" and answer.reason == ARCHITECT_PUBLISH_FAILED
    world.db.commit.assert_awaited_once()
    assert world.story.status == "reopened" and world.task.status == "cancelled"


@pytest.mark.asyncio
async def test_a_repeated_replan_is_refused_without_a_second_effect(world):
    await replan(world)
    world.db.add.reset_mock()

    with pytest.raises(HTTPException) as refused:
        await replan(world)

    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "install_task_not_done"
    world.db.add.assert_not_called()
    world.db.commit.assert_awaited_once()
    world.redis.publish_message.assert_awaited_once()


def _foreign_operation(world):
    return {"operation_id": "install-other"}


def _refused_operation(world):
    world.task.install_operation = world.operation.model_copy(
        update={"state": "refused"}
    ).model_dump(mode="json")


def _task_in_dev(world):
    world.task.status = "in_dev"


def _story_in_progress(world):
    world.story.status = "in_progress"


def _scaffolder_stop(world):
    world.cause["source"] = "scaffolder"


def _other_detail(world):
    world.cause["detail"] = "Catalog install install-1 at push: rejected."


def _no_cause(world):
    world.story.quarantine_reason = None


def _other_stop_id(world):
    return {"stop_id": "stop-other"}


def _released_stop(world):
    world.story.engineering_stop["released_at"] = world.now.isoformat()
    world.story.engineering_stop["release_actor"] = "user:admin"


def _no_stop(world):
    world.story.engineering_stop = None


def _older_cycle(world):
    world.story.reopened_at = world.now


def _branch_present(world):
    world.github.head = "c" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        (_foreign_operation, "stale_install_operation"),
        (_refused_operation, "install_operation_not_published"),
        (_task_in_dev, "install_task_not_done"),
        (_story_in_progress, "story_not_waiting_human_review"),
        (_scaffolder_stop, "unrelated_story_stop"),
        (_other_detail, "unrelated_story_stop"),
        (_no_cause, "unrelated_story_stop"),
        (_other_stop_id, "engineering_stop_mismatch"),
        (_released_stop, "engineering_stop_mismatch"),
        (_no_stop, "engineering_stop_mismatch"),
        (_older_cycle, "stale_install_cycle"),
        (_branch_present, "install_branch_present"),
    ],
)
async def test_replan_refuses_every_miss_and_changes_nothing(world, arrange, code):
    overrides = arrange(world) or {}
    task_before = dict(vars(world.task))
    story_before = {
        key: value.copy() if isinstance(value, dict) else value
        for key, value in vars(world.story).items()
    }

    with pytest.raises(HTTPException) as refused:
        await replan(world, **overrides)

    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == code
    if code == "install_branch_present":
        assert "delete the remote branch story/story-1" in refused.value.detail["detail"]
    assert vars(world.task) == task_before and vars(world.story) == story_before
    world.db.add.assert_not_called()
    world.db.commit.assert_not_awaited()
    world.redis.publish_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_replan_refuses_a_task_without_an_install_operation(world):
    world.task.install_operation = None

    with pytest.raises(HTTPException) as refused:
        await replan(world)

    assert refused.value.detail["code"] == "install_operation_missing"
    world.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_bearer_admin_route_replans_with_the_api_queue_client(world):
    from httpx import ASGITransport, AsyncClient
    from internal_caller import INTERNAL_HEADERS

    from src.database import get_async_session
    from src.dependencies import get_redis_client, require_bearer_admin
    from src.main import app

    async def session():
        yield world.db

    app.dependency_overrides[get_async_session] = session
    app.dependency_overrides[get_redis_client] = lambda: world.redis
    app.dependency_overrides[require_bearer_admin] = lambda: SimpleNamespace(id=7)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            response = await client.post(
                f"/api/tasks/{world.task.id}/catalog-install/recovery",
                json=request(world).model_dump(mode="json"),
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "settled"
    assert world.story.engineering_stop["release_actor"] == "user:7"
    world.redis.publish_message.assert_awaited_once()
