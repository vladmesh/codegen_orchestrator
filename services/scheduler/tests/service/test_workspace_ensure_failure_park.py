"""Real API proof that a failed ensure-workspace parks loudly and recovers in one action.

The governing reproduction is 2026-09-14: story teardown cleared `workspace_ready`,
ensure failed and recorded `scaffold_error`, the scaffold trigger went quiet, and
admission refused the story's todo task with `workspace_not_ready` every tick
while nobody was told. Here the same sequence must end, within one dispatcher
tick, in a typed park with one owed notice per audience, no further refusals,
and one operator retry that lets ensure run again and dispatches the task.
"""

from datetime import UTC, datetime
import uuid

import pytest
import structlog

from shared.contracts.dto.engineering_dispatch import EngineeringDispatchRefusal
from shared.contracts.dto.engineering_execution import ENGINEERING_INFRASTRUCTURE_KEY
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.user import UserDTO
from shared.queues import SCAFFOLD_QUEUE
from src.tasks import scaffold_trigger
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.task_dispatcher import dispatch_todo_tasks

WORKSPACE_REFUSAL = "workspace_ensure_failed"
SCAFFOLD_ERROR = "Git clone failed: repository not found"


class _InflightKeys:
    """The two Redis calls the scaffold trigger and the scaffolder make on its dedup key."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, _value: str, *, nx: bool, ex: int) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, key: str) -> None:
        self.keys.discard(key)


class _RecordingRedis:
    """Every queue accepts messages and records them, keyed by queue."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.owner_events: list[dict] = []
        self.redis = _InflightKeys()

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"

    async def publish_flat(self, queue: str, fields: dict) -> str:
        self.owner_events.append(fields)
        return "1-1"


class _AdminChannel:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def admin_users(self) -> list[UserDTO]:
        return [UserDTO(id=1, telegram_id=5001, is_admin=True, created_at=datetime.now(UTC))]

    async def send_telegram_message(
        self, telegram_id: int, text: str, parse_mode: str = "Markdown"
    ) -> bool:
        self.messages.append(text)
        return True


class _Pipeline:
    """The scheduler loops and the scaffolder's project writes, driven one step at a time."""

    def __init__(self, api_client, monkeypatch, project_id: str, story_id: str, task_id: str):
        self.api = api_client
        self.project_id = project_id
        self.story_id = story_id
        self.task_id = task_id
        self.redis = _RecordingRedis()
        self.admins = _AdminChannel()
        self.decisions: list = []
        admit = api_client.admit_engineering_dispatch

        async def recording_admission(command):
            decision = await admit(command)
            if command.task_id == task_id:
                self.decisions.append(decision)
            return decision

        monkeypatch.setattr(api_client, "admit_engineering_dispatch", recording_admission)
        monkeypatch.setattr(scaffold_trigger, "_scaffold_inflight_ttl", lambda: 600)
        monkeypatch.setattr(
            scaffold_trigger,
            "_template_config",
            # Any ref: the ensure message is only recorded, never rendered, and the
            # production pin is a literal in exactly one file.
            lambda: ("gh:vladmesh/codegen-product-kit", "test-template-ref"),
        )
        monkeypatch.setattr("shared.notifications._list_admin_users", self.admins.admin_users)
        monkeypatch.setattr(
            "shared.notifications.send_telegram_message", self.admins.send_telegram_message
        )

    async def tick(self) -> None:
        await dispatch_todo_tasks(self.api, self.redis)

    async def trigger_ensure(self) -> bool:
        project = await self.api.get_project(self.project_id)
        return await scaffold_trigger._trigger_ensure_scaffold(
            project, self.api, self.redis, structlog.get_logger()
        )

    def ensure_messages(self) -> list:
        return [
            message
            for queue, message in self.redis.messages
            if queue == SCAFFOLD_QUEUE and message.project_id == self.project_id
        ]

    def engineering_messages(self) -> list:
        return [
            message
            for queue, message in self.redis.messages
            if queue != SCAFFOLD_QUEUE
            and getattr(message, "planning_task_id", None) == self.task_id
        ]

    async def _finish_scaffold_job(self, config: dict) -> None:
        response = await self.api.request(
            "PATCH", f"projects/{self.project_id}", json={"config": config}
        )
        assert response.is_success, response.text
        # The scaffolder consumer clears the dedup key once its job is acked.
        await self.redis.redis.delete(f"{scaffold_trigger.SCAFFOLD_INFLIGHT_KEY}:{self.project_id}")

    async def ensure_fails(self) -> None:
        """The scaffolder's `_record_scaffold_error` write for a failed ensure."""
        config = dict((await self.api.get_project(self.project_id)).config or {})
        config["scaffold_error"] = SCAFFOLD_ERROR
        await self._finish_scaffold_job(config)

    async def ensure_succeeds(self) -> None:
        """The scaffolder's `_update_project_on_success` write."""
        config = dict((await self.api.get_project(self.project_id)).config or {})
        config.update({"tree": ".", "workspace_ready": True})
        config.pop("scaffold_error", None)
        await self._finish_scaffold_job(config)

    async def config(self) -> dict:
        return dict((await self.api.get_project(self.project_id)).config or {})

    async def park(self) -> dict:
        task = await self.api.get_task(self.task_id)
        return (task.failure_metadata or {}).get(ENGINEERING_INFRASTRUCTURE_KEY)

    async def retry(self, park: dict):
        return await self.api.request(
            "POST",
            f"stories/{self.story_id}/retry-infrastructure-attempt",
            json={
                "task_id": self.task_id,
                "attempt_id": park["attempt_id"],
                "refusal": park["refusal"],
                "actor": "admin",
            },
        )

    async def deliver_notices(self) -> None:
        await supervise_owed_owner_notifications(self.api, self.redis)

    def owner_notices(self) -> list[dict]:
        return [e for e in self.redis.owner_events if e.get("story_id") == self.story_id]

    def admin_notices(self) -> list[str]:
        return [text for text in self.admins.messages if self.story_id in text]

    def refusals(self, reason: EngineeringDispatchRefusal) -> list:
        return [decision for decision in self.decisions if decision.reason is reason]


async def _torn_down_story(api_client, monkeypatch) -> _Pipeline:
    """An ACTIVE project whose story's todo task lost its workspace to teardown."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created_user = await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"ws-park-{telegram_id}"},
    )
    assert created_user.is_success, created_user.text
    project_id = str(uuid.uuid4())
    created_project = await api_client.request(
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Workspace ensure park",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True, "modules": ["backend"]},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created_project.is_success, created_project.text
    repository = await api_client.request(
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": "Workspace ensure park",
            "git_url": f"https://github.com/test-org/ws-park-{telegram_id}",
        },
    )
    assert repository.is_success, repository.text
    created_story = await api_client.request(
        "POST", "stories/", json={"project_id": project_id, "title": "Torn-down story"}
    )
    assert created_story.is_success, created_story.text
    story_id = created_story.json()["id"]
    task = await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story_id,
            "type": "feature",
            "title": "Waits for its workspace",
            "status": "todo",
        }
    )
    await api_client.transition_story(story_id, "start")
    pipeline = _Pipeline(api_client, monkeypatch, project_id, story_id, task.id)

    # Story teardown: the worker-manager GC reports the deleted workspace.
    deleted = await api_client.request(
        "POST", f"repositories/{repository.json()['id']}/notify-workspace-deleted"
    )
    assert deleted.is_success, deleted.text
    assert "workspace_ready" not in await pipeline.config()
    return pipeline


async def _parked_after_failed_ensure(pipeline: _Pipeline) -> dict:
    """Ensure is triggered, fails, and the next dispatcher tick parks the story."""
    assert await pipeline.trigger_ensure()
    assert [message.mode for message in pipeline.ensure_messages()] == ["ensure"]
    # While ensure is in flight the workspace is simply not ready yet.
    await pipeline.tick()
    assert len(pipeline.refusals(EngineeringDispatchRefusal.WORKSPACE_NOT_READY)) == 1

    await pipeline.ensure_fails()
    assert (await pipeline.config())["scaffold_error"] == SCAFFOLD_ERROR
    assert not await pipeline.trigger_ensure()

    await pipeline.tick()

    park = await pipeline.park()
    assert park is not None
    assert (park["refusal"], park["task_id"]) == (WORKSPACE_REFUSAL, pipeline.task_id)
    assert SCAFFOLD_ERROR in park["detail"]
    assert pipeline.decisions[-1].reason is EngineeringDispatchRefusal.WORKSPACE_ENSURE_FAILED
    task = await pipeline.api.get_task(pipeline.task_id)
    story = await pipeline.api.get_story(pipeline.story_id)
    assert (task.status, task.current_iteration) == (TaskStatus.WAITING_HUMAN_REVIEW, 0)
    assert story.status is StoryStatus.WAITING_HUMAN_REVIEW
    assert story.quarantine_reason == {ENGINEERING_INFRASTRUCTURE_KEY: park}
    return park


@pytest.mark.asyncio
async def test_a_failed_ensure_parks_the_story_once_and_stops_the_refusal_loop(
    api_client, monkeypatch
):
    pipeline = await _torn_down_story(api_client, monkeypatch)

    park = await _parked_after_failed_ensure(pipeline)

    notice = await api_client.get_story_owner_notification(pipeline.story_id)
    assert (notice.state, notice.admin_state) == (
        OwnerNotificationState.OWED,
        OwnerNotificationState.OWED,
    )
    assert notice.text == park["detail"]
    decided = len(pipeline.decisions)

    for _ in range(3):
        await pipeline.tick()
        assert not await pipeline.trigger_ensure()

    assert len(pipeline.decisions) == decided
    assert len(pipeline.refusals(EngineeringDispatchRefusal.WORKSPACE_NOT_READY)) == 1
    assert await pipeline.park() == park
    assert pipeline.engineering_messages() == []
    assert (
        await api_client.list_runs(task_id=pipeline.task_id, run_type=RunType.ENGINEERING.value)
        == []
    )

    await pipeline.deliver_notices()
    await pipeline.deliver_notices()
    assert (len(pipeline.owner_notices()), len(pipeline.admin_notices())) == (1, 1)


@pytest.mark.asyncio
async def test_one_retry_lets_ensure_run_again_and_dispatches_the_task(api_client, monkeypatch):
    pipeline = await _torn_down_story(api_client, monkeypatch)
    park = await _parked_after_failed_ensure(pipeline)
    config_before = await pipeline.config()

    retried = await pipeline.retry(park)

    assert retried.is_success, retried.text
    assert retried.json()["outcome"] == "retried"
    expected_config = {k: v for k, v in config_before.items() if k != "scaffold_error"}
    assert await pipeline.config() == expected_config
    assert (await api_client.get_story(pipeline.story_id)).status is StoryStatus.IN_PROGRESS
    task = await api_client.get_task(pipeline.task_id)
    assert (task.status, task.current_iteration, task.failure_metadata) == (
        TaskStatus.TODO,
        0,
        None,
    )
    events = await api_client.get_task_events(pipeline.task_id)

    repeated = await pipeline.retry(park)

    assert repeated.is_success, repeated.text
    assert repeated.json()["outcome"] == "already_retried"
    assert await api_client.get_task_events(pipeline.task_id) == events
    assert await pipeline.config() == expected_config

    assert await pipeline.trigger_ensure()
    assert len(pipeline.ensure_messages()) == 2
    await pipeline.ensure_succeeds()
    await pipeline.tick()

    runs = await api_client.list_runs(task_id=pipeline.task_id, run_type=RunType.ENGINEERING.value)
    assert [run.status for run in runs] == [RunStatus.QUEUED]
    assert len(pipeline.engineering_messages()) == 1
    assert (await api_client.get_task(pipeline.task_id)).status == TaskStatus.IN_DEV
    assert pipeline.decisions[-1].outcome.value == "admitted"


@pytest.mark.asyncio
async def test_a_second_ensure_failure_after_recovery_parks_again_with_one_new_notice(
    api_client, monkeypatch
):
    pipeline = await _torn_down_story(api_client, monkeypatch)
    first = await _parked_after_failed_ensure(pipeline)
    await pipeline.deliver_notices()
    assert (len(pipeline.owner_notices()), len(pipeline.admin_notices())) == (1, 1)
    retried = await pipeline.retry(first)
    assert retried.json()["outcome"] == "retried", retried.text

    assert await pipeline.trigger_ensure()
    await pipeline.ensure_fails()
    await pipeline.tick()

    second = await pipeline.park()
    assert second is not None
    assert second["refusal"] == WORKSPACE_REFUSAL
    assert second["attempt_id"] != first["attempt_id"]
    story = await api_client.get_story(pipeline.story_id)
    assert story.status is StoryStatus.WAITING_HUMAN_REVIEW
    decided = len(pipeline.decisions)
    for _ in range(2):
        await pipeline.tick()
        assert not await pipeline.trigger_ensure()
    assert len(pipeline.decisions) == decided

    await pipeline.deliver_notices()
    await pipeline.deliver_notices()
    assert (len(pipeline.owner_notices()), len(pipeline.admin_notices())) == (2, 2)
    assert pipeline.engineering_messages() == []
