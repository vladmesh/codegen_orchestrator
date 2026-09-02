"""Real API proof that a pre-handoff abort is dispatchable on the next tick."""

import uuid

import pytest

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchCommand,
    EngineeringDispatchOutcome,
)
from shared.contracts.dto.run import RunStatus, RunType
from src.tasks.task_dispatcher import dispatch_todo_tasks


class _RecordingRedis:
    """The dispatcher needs only a successful queue boundary for this proof."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"


@pytest.mark.asyncio
async def test_real_pre_handoff_abort_is_readable_and_dispatches_a_new_attempt(api_client):
    """The actual API/client/dispatcher path never needs a fabricated terminal Run."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    created_user = await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"abort-recovery-{telegram_id}"},
    )
    assert created_user.is_success, created_user.text
    created_project = await api_client.request(
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Abort recovery",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created_project.is_success, created_project.text
    task = await api_client.create_task(
        {
            "project_id": project_id,
            "type": "feature",
            "title": "Dispatch after abort",
            "status": "todo",
        }
    )

    # The prior attempt is created the one way a paid engineering attempt on a
    # Task can be created: through the declared admission point. It is then
    # aborted before the queue handoff, which is the state this test is about.
    admitted = await api_client.admit_engineering_dispatch(
        EngineeringDispatchCommand(task_id=task.id)
    )
    assert admitted.outcome is EngineeringDispatchOutcome.ADMITTED, admitted
    aborted_id = admitted.run_id
    assert aborted_id is not None
    await api_client.abort_paid_run_pre_handoff(aborted_id, "recipient preparation failed")

    before_tick = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    assert [(run.id, run.status, run.result) for run in before_tick] == [
        (aborted_id, RunStatus.CANCELLED, None)
    ]
    assert before_tick[0].run_metadata["pre_handoff_aborted"] is True

    redis = _RecordingRedis()
    assert await dispatch_todo_tasks(api_client, redis) == 1

    after_tick = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    fresh = [run for run in after_tick if run.id != aborted_id]
    assert len(fresh) == 1
    assert fresh[0].status is RunStatus.QUEUED
    assert fresh[0].run_metadata.get("pre_handoff_aborted") is None
    assert len(redis.messages) == 1
