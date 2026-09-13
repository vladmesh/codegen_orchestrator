"""Real API proof that an admission refusal parks once and one retry recovers it.

The governing reproduction: the first dispatcher tick is refused with
`executor_unavailable` and the owner's notice cannot be published. The park must
still be complete after that tick, the next tick must neither admit nor publish
anything for the task, and one operator call must return it to one fresh attempt.
"""

from datetime import UTC, datetime, timedelta
import os
import uuid

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
from shared.contracts.vocab import AgentType
from src.tasks.owner_notifications import supervise_owed_owner_notifications
from src.tasks.task_dispatcher import dispatch_todo_tasks


class _RecordingRedis:
    """The engineering queue accepts messages; the owner's queue is unreachable."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"

    async def publish_flat(self, queue: str, fields: dict) -> str:
        raise ConnectionError("po:input is unreachable")


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
async def test_refused_admission_parks_once_and_one_retry_restores_one_attempt(
    api_client, monkeypatch
):
    story_id, task_id = await _story_task(api_client)
    admitted_task_ids: list[str] = []
    admit = api_client.admit_engineering_dispatch

    async def recording_admission(command):
        admitted_task_ids.append(command.task_id)
        return await admit(command)

    monkeypatch.setattr(api_client, "admit_engineering_dispatch", recording_admission)
    redis = _RecordingRedis()

    def published_for_task() -> list:
        return [message for _, message in redis.messages if message.planning_task_id == task_id]

    await _publish_executors(ExecutorAvailability.UNAVAILABLE, "local_auth_invalid")
    try:
        # Tick one: admission refuses, the park commits, the owner publish fails.
        await dispatch_todo_tasks(api_client, redis)
        await supervise_owed_owner_notifications(api_client, redis)

        task = await api_client.get_task(task_id)
        story = await api_client.get_story(story_id)
        evidence = task.failure_metadata[ENGINEERING_INFRASTRUCTURE_KEY]
        assert task.status == TaskStatus.WAITING_HUMAN_REVIEW
        assert task.current_iteration == 0
        assert story.status is StoryStatus.WAITING_HUMAN_REVIEW
        assert story.quarantine_reason == {ENGINEERING_INFRASTRUCTURE_KEY: evidence}
        assert (evidence["task_id"], evidence["refusal"]) == (task_id, "executor_unavailable")
        notice = await api_client.get_story_owner_notification(story_id)
        assert (notice.state, notice.attempts) == (OwnerNotificationState.OWED, 1)

        # Tick two: nothing admits, mints, or publishes for the parked task.
        await dispatch_todo_tasks(api_client, redis)
        assert admitted_task_ids.count(task_id) == 1
        assert published_for_task() == []
        assert await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value) == []
        assert (await api_client.get_task(task_id)).status == TaskStatus.WAITING_HUMAN_REVIEW
    finally:
        await _publish_executors(ExecutorAvailability.AVAILABLE, "ready")

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
