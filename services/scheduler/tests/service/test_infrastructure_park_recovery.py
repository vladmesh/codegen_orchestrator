"""Real API proof that an admission refusal is a park even when its answer is lost.

The governing reproduction: the first dispatcher tick is refused with
`executor_unavailable` and the HTTP answer never reaches the scheduler. The park,
its evidence and both notice audiences must already be committed; the next tick
must neither admit, mint, nor publish anything for the task; owner and
administrator delivery must retry independently and settle once across a
restarted sweep; and one operator call must return the task to one fresh attempt.
"""

from datetime import UTC, datetime, timedelta
import os
import uuid

import httpx
import pytest
from redis.asyncio import Redis

from shared.contracts.dto.engineering_execution import ENGINEERING_INFRASTRUCTURE_KEY
from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.user import UserDTO
from shared.contracts.vocab import AgentType
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.task_dispatcher import dispatch_todo_tasks


class _RecordingRedis:
    """The engineering queue accepts messages; the owner's queue fails on demand."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.owner_events: list[dict] = []
        self.owner_failures = 0

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"

    async def publish_flat(self, queue: str, fields: dict) -> str:
        if self.owner_failures:
            self.owner_failures -= 1
            raise ConnectionError("po:input is unreachable")
        self.owner_events.append(fields)
        return "1-1"


class _AdminChannel:
    """Telegram for administrators, refusing on demand the way production does.

    Only `send_telegram_message` and the administrator list are replaced; the
    real `deliver_to_admins` aggregates them. A refused send returns `False`.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failures = 0

    async def admin_users(self) -> list[UserDTO]:
        return [UserDTO(id=1, telegram_id=5001, is_admin=True, created_at=datetime.now(UTC))]

    async def send_telegram_message(
        self, telegram_id: int, text: str, parse_mode: str = "Markdown"
    ) -> bool:
        if self.failures:
            self.failures -= 1
            return False
        self.messages.append(text)
        return True


async def _publish_executors(availability: ExecutorAvailability, reason_code: str) -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(hours=1)
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        await redis.set(
            EXECUTOR_DIAGNOSTICS_REDIS_KEY,
            ExecutorDiagnosticSnapshot(
                schema_version="v1",
                version=f"infrastructure-park-{reason_code}-{uuid.uuid4().hex[:8]}",
                observed_at=now,
                expires_at=expiry,
                diagnostics=[
                    ExecutorDiagnostic(
                        executor=executor,
                        enabled=True,
                        auth_mode=ExecutorAuthMode.HOST_SESSION,
                        availability=availability,
                        observed_at=now,
                        expires_at=expiry,
                        active_lease_count=0,
                        reason_code=reason_code,
                        reason=safe_executor_diagnostic_reason(reason_code),
                    )
                    for executor in (AgentType.CLAUDE, AgentType.CODEX)
                ],
            ).model_dump_json(),
            ex=3600,
        )
    finally:
        await redis.aclose()


async def _story_task(api_client) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created_user = await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"infra-park-{telegram_id}"},
    )
    assert created_user.is_success, created_user.text
    project_id = str(uuid.uuid4())
    created_project = await api_client.request(
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Infrastructure park recovery",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created_project.is_success, created_project.text
    created_story = await api_client.request(
        "POST", "stories/", json={"project_id": project_id, "title": "Refused story"}
    )
    assert created_story.is_success, created_story.text
    story_id = created_story.json()["id"]
    task = await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story_id,
            "type": "feature",
            "title": "Refused by the executor",
            "status": "todo",
        }
    )
    await api_client.transition_story(story_id, "start")
    return story_id, task.id


@pytest.mark.asyncio
async def test_a_lost_refusal_answer_leaves_a_recoverable_park_and_both_notices(  # noqa: PLR0915
    api_client, monkeypatch
):
    story_id, task_id = await _story_task(api_client)
    asked_for_task: list[str] = []
    admit = api_client.admit_engineering_dispatch

    async def admission_whose_first_answer_is_lost(command):
        decision = await admit(command)
        if command.task_id == task_id:
            asked_for_task.append(command.task_id)
            if len(asked_for_task) == 1:
                raise httpx.ReadTimeout("the refusal committed but its answer never arrived")
        return decision

    monkeypatch.setattr(
        api_client, "admit_engineering_dispatch", admission_whose_first_answer_is_lost
    )
    admins = _AdminChannel()
    monkeypatch.setattr("shared.notifications._list_admin_users", admins.admin_users)
    monkeypatch.setattr("shared.notifications.send_telegram_message", admins.send_telegram_message)
    redis = _RecordingRedis()

    def published_for_task() -> list:
        return [message for _, message in redis.messages if message.planning_task_id == task_id]

    def owner_events(stream: _RecordingRedis) -> list[dict]:
        return [event for event in stream.owner_events if event.get("story_id") == story_id]

    def admin_messages() -> list[str]:
        return [message for message in admins.messages if story_id in message]

    await _publish_executors(ExecutorAvailability.UNAVAILABLE, "local_auth_invalid")
    try:
        # Tick one: admission refuses and parks; the scheduler never hears back.
        await dispatch_todo_tasks(api_client, redis)

        task = await api_client.get_task(task_id)
        story = await api_client.get_story(story_id)
        evidence = task.failure_metadata[ENGINEERING_INFRASTRUCTURE_KEY]
        assert task.status == TaskStatus.WAITING_HUMAN_REVIEW
        assert task.current_iteration == 0
        assert story.status is StoryStatus.WAITING_HUMAN_REVIEW
        assert story.quarantine_reason == {ENGINEERING_INFRASTRUCTURE_KEY: evidence}
        assert (evidence["task_id"], evidence["refusal"]) == (task_id, "executor_unavailable")

        # Tick two: nothing admits, mints, or publishes for the parked task.
        await dispatch_todo_tasks(api_client, redis)
        assert asked_for_task == [task_id]
        assert published_for_task() == []
        assert await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value) == []
        assert (await api_client.get_task(task_id)).status == TaskStatus.WAITING_HUMAN_REVIEW
    finally:
        await _publish_executors(ExecutorAvailability.AVAILABLE, "ready")

    # Both audiences fail once; a restarted sweep then settles each exactly once.
    redis.owner_failures = 1
    admins.failures = 1
    await supervise_owed_owner_notifications(api_client, redis)
    notice = await api_client.get_story_owner_notification(story_id)
    assert (notice.state, notice.attempts) == (OwnerNotificationState.OWED, 1)
    assert (notice.admin_state, notice.admin_attempts) == (OwnerNotificationState.OWED, 1)
    assert owner_events(redis) == []
    assert admin_messages() == []

    restarted = _RecordingRedis()
    await supervise_owed_owner_notifications(api_client, restarted)
    await supervise_owed_owner_notifications(api_client, restarted)
    notice = await api_client.get_story_owner_notification(story_id)
    assert notice.state is OwnerNotificationState.DELIVERED
    assert notice.admin_state is OwnerNotificationState.DELIVERED
    assert len(owner_events(restarted)) == 1
    assert len(admin_messages()) == 1

    retried = await api_client.request(
        "POST",
        f"stories/{story_id}/retry-infrastructure-attempt",
        json={
            "task_id": task_id,
            "attempt_id": evidence["attempt_id"],
            "refusal": evidence["refusal"],
            "actor": "admin",
        },
    )
    assert retried.is_success, retried.text
    assert retried.json()["outcome"] == "retried"
    task = await api_client.get_task(task_id)
    assert (task.status, task.current_iteration, task.failure_metadata) == (
        TaskStatus.TODO,
        0,
        None,
    )
    assert (await api_client.get_story(story_id)).status is StoryStatus.IN_PROGRESS

    await dispatch_todo_tasks(api_client, redis)

    runs = await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value)
    assert [run.status for run in runs] == [RunStatus.QUEUED]
    assert runs[0].id != evidence["attempt_id"]
    assert len(published_for_task()) == 1
    assert (await api_client.get_task(task_id)).status == TaskStatus.IN_DEV
    # Recovery neither re-owes nor resends a settled audience.
    await supervise_owed_owner_notifications(api_client, restarted)
    assert len(owner_events(restarted)) == 1
    assert len(admin_messages()) == 1
