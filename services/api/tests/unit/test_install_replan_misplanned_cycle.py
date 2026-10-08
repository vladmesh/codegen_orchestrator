"""A replanned install story whose new cycle was planned as engineering is replanned again.

The second shape of `replan`: the call names the install Task a previous replan
cancelled. While the story has not been reopened since, is stopped for review, runs
nothing and has neither an install nor a finished Task in the cycle that replan
opened, the cycle is discarded in one transaction and the architect is asked again.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
import pytest
from test_install_contract import install_payload
from test_install_replan import FakeGitHub

from shared.contracts.dto.catalog_install import (
    REPLAN_REOPEN_WINDOW,
    InstallOperation,
    InstallOperatorRequest,
)
from shared.contracts.queues.architect import ArchitectMessage
from shared.models import Run, TaskEvent, WorkAdmissionAudit
from shared.queues import ARCHITECT_QUEUE
from src.catalog_install_recovery import operator_install_recovery

STOP_ID = "stop-po"
CHAT_ID = "4242"


class World(SimpleNamespace):
    """A story stopped in its replanned cycle, its Tasks, and every fake boundary."""


def _row(**fields):
    return SimpleNamespace(**fields)


class FakeDB:
    """The rows `replan` reads under its locks: the install Task's notes and the story's runs."""

    def __init__(self, repository, notes, runs, calls):
        self.repository = repository
        self.notes = notes
        self.runs = runs
        self.calls = calls
        self.add = Mock()

    async def scalar(self, _statement):
        return self.repository

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = {TaskEvent: self.notes, Run: self.runs}[entity]
        return SimpleNamespace(all=lambda: list(rows))

    async def commit(self):
        self.calls.append("commit")


@pytest.fixture
def world(monkeypatch):
    now = datetime.now(UTC)
    created = now - timedelta(days=1)
    reopened = now - timedelta(minutes=30)
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
    install = _row(
        id=operation.task_id,
        type="install",
        project_id=pid,
        story_id=operation.story_id,
        repository_id=operation.repository_id,
        status="cancelled",
        created_at=created,
        install=install_payload(),
        install_operation=operation.model_dump(mode="json"),
    )
    # What the replanned cycle was misplanned as: an engineering fix the PO stopped.
    fix = _row(
        id="task-fix",
        type="fix",
        project_id=pid,
        story_id=operation.story_id,
        status="in_dev",
        created_at=reopened + timedelta(seconds=4),
        install_operation=None,
    )
    story = _row(
        id=operation.story_id,
        project_id=pid,
        created_at=created,
        reopened_at=reopened,
        status="waiting_human_review",
        waiting_on="human_review",
        status_entered_at=now,
        quarantine_reason=None,
        engineering_stop={"id": STOP_ID, "actor": "user:po", "stopped_at": now.isoformat()},
    )
    repository = _row(
        project_id=pid, is_managed=True, name="notes", git_url="https://github.com/synthetic/notes"
    )
    # The first replan's note: written at its transaction's start, just before the reopen.
    note = _row(
        id=11,
        task_id=install.id,
        event_type="note",
        created_at=reopened - timedelta(milliseconds=400),
        details={
            "catalog_install_settlement": operation.model_dump(mode="json"),
            "operator_action": "replan",
        },
    )
    calls = []
    github = FakeGitHub(None)
    db = FakeDB(repository, [note], [_row(id="eng-1", status="failed")], calls)
    redis = SimpleNamespace(
        publish_message=AsyncMock(side_effect=lambda *_: calls.append("publish"))
    )
    roster = {install.id: install, fix.id: fix}
    monkeypatch.setattr(
        "src.catalog_install_recovery._lock_dispatch_tasks",
        AsyncMock(side_effect=lambda *_: (install, roster, None, story.id)),
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
        reopened=reopened,
        operation=operation,
        install=install,
        fix=fix,
        roster=roster,
        story=story,
        note=note,
        github=github,
        db=db,
        redis=redis,
        calls=calls,
    )


async def replan(world, **overrides):
    fields = {"operation_id": world.operation.id, "action": "replan", "stop_id": STOP_ID}
    return await operator_install_recovery(
        world.install.id,
        InstallOperatorRequest(**{**fields, **overrides}),
        "user:admin",
        world.db,
        redis=world.redis,
    )


def added(world, kind):
    return [call.args[0] for call in world.db.add.call_args_list if isinstance(call.args[0], kind)]


@pytest.mark.asyncio
async def test_the_misplanned_cycle_is_discarded_and_the_architect_asked_again(world):
    answer = await replan(world)

    assert answer.outcome == "settled" and answer.reason is None
    assert world.story.engineering_stop["released_at"] is not None
    assert world.story.engineering_stop["release_actor"] == "user:admin"
    assert [audit.outcome for audit in added(world, WorkAdmissionAudit)] == ["released"]
    events = added(world, TaskEvent)
    hops = [
        (e.task_id, e.from_status, e.to_status) for e in events if e.event_type == "status_change"
    ]
    assert hops == [("task-fix", "in_dev", "cancelled")]
    assert world.fix.status == "cancelled"
    # The install Task stays the cancelled handle, and carries the new replan note.
    assert world.install.status == "cancelled"
    assert [(e.task_id, e.details) for e in events if e.event_type == "note"] == [
        (
            "task-install",
            {
                "catalog_install_settlement": world.operation.model_dump(mode="json"),
                "operator_action": "replan",
                "cancelled_task_ids": ["task-fix"],
            },
        )
    ]
    assert world.story.status == "reopened" and world.story.waiting_on == "none"
    assert world.story.reopened_at > world.reopened
    assert world.story.quarantine_reason is None
    assert world.github.refs == [("synthetic", "notes", "heads/story/story-1")]
    assert world.calls == ["commit", "publish"]
    queue, message = world.redis.publish_message.await_args.args
    assert queue == ARCHITECT_QUEUE and isinstance(message, ArchitectMessage)
    assert message.story_id == "story-1" and message.telegram_chat_id == CHAT_ID
    assert message.is_reopen and message.user_report is None


@pytest.mark.asyncio
async def test_a_stop_with_a_recorded_cause_is_released_with_that_cause(world):
    cause = {"reason": "operator_review", "source": "api", "detail": "PO stopped the run"}
    world.story.quarantine_reason = cause

    answer = await replan(world)

    assert answer.outcome == "settled"
    assert world.story.engineering_stop["released_at"] is not None
    assert world.story.quarantine_reason is None and world.story.status == "reopened"


@pytest.mark.asyncio
async def test_a_repeated_replan_is_refused_without_a_second_effect(world):
    await replan(world)
    world.db.add.reset_mock()

    with pytest.raises(HTTPException) as refused:
        await replan(world)

    assert refused.value.status_code == 409
    # The story left review: the call is the first shape's again, whose Task is not done.
    assert refused.value.detail["code"] == "install_task_not_done"
    world.db.add.assert_not_called()
    assert world.calls == ["commit", "publish"]


def _no_replan_note(world):
    world.db.notes = [
        _row(**{**vars(world.note), "details": {"catalog_install_settlement": {}, "x": 1}})
    ]


def _reopened_since(world):
    world.story.reopened_at = world.note.created_at + REPLAN_REOPEN_WINDOW + timedelta(seconds=1)


def _never_reopened(world):
    world.story.reopened_at = None


def _foreign_operation(world):
    return {"operation_id": "install-other"}


def _other_stop_id(world):
    return {"stop_id": "stop-other"}


def _released_stop(world):
    world.story.engineering_stop["released_at"] = world.now.isoformat()
    world.story.engineering_stop["release_actor"] = "user:admin"


def _no_stop(world):
    world.story.engineering_stop = None


def _running(world):
    world.db.runs = [_row(id="eng-2", status="running")]


def _queued(world):
    world.db.runs = [_row(id="eng-2", status="queued")]


def _cycle_install(world):
    world.roster["task-install-new"] = _row(
        id="task-install-new",
        type="install",
        story_id="story-1",
        status="cancelled",
        created_at=world.reopened + timedelta(seconds=5),
    )


def _cycle_done(world):
    world.fix.status = "done"


def _branch_present(world):
    world.github.head = "c" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        (_no_replan_note, "install_task_not_replanned"),
        (_reopened_since, "story_reopened_since_replan"),
        (_never_reopened, "story_reopened_since_replan"),
        (_foreign_operation, "stale_install_operation"),
        (_other_stop_id, "engineering_stop_mismatch"),
        (_released_stop, "engineering_stop_mismatch"),
        (_no_stop, "engineering_stop_mismatch"),
        (_running, "story_run_live"),
        (_queued, "story_run_live"),
        (_cycle_install, "cycle_has_install"),
        (_cycle_done, "cycle_has_done_task"),
        (_branch_present, "install_branch_present"),
    ],
)
async def test_every_miss_is_its_own_409_and_changes_nothing(world, arrange, code):
    overrides = arrange(world) or {}
    rows = [world.install, world.fix, world.story]
    before = [
        {
            key: value.copy() if isinstance(value, dict) else value
            for key, value in vars(row).items()
        }
        for row in rows
    ]

    with pytest.raises(HTTPException) as refused:
        await replan(world, **overrides)

    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == code
    assert [vars(row) for row in rows] == before
    world.db.add.assert_not_called()
    assert world.calls == []
    world.redis.publish_message.assert_not_awaited()
