"""The API's resource-wait actions, for routing tests that drive a bare ``AsyncMock``.

The real actions are one locked transaction each (proved against Postgres in the
API and scheduler service tests). This double answers them the way they answer:
it keeps each task's status and wait facts, decides a new wait from whether a
wait start is already recorded, mints the owed record from the story the client
reads back and the task it holds, and hands that record to ``ClaimsFromWrites``
as the Run's current one — so the scheduler's in-tick delivery meets the record
through the real seam, including its task-status check, which ``get_task``
answers from the same state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from _owner_notification_claims import ClaimsFromWrites

from shared.contracts.dto.lifecycle_wait import (
    RESOURCE_WAIT_TASK_STATUSES,
    RESOURCES_RESUMED_TASK_STATUSES,
    TaskResourceResumeCommand,
    TaskResourceResumeDisposition,
    TaskResourceResumeRead,
    TaskResourceWaitCommand,
    TaskResourceWaitDisposition,
    TaskResourceWaitRead,
)
from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.task import TaskDTO, TaskStatus
from shared.contracts.vocab import OwnerNotificationEvent


class ResourceWaitDouble:
    """Park and resume on tracked tasks; every other call is the mock's own."""

    def __init__(self, client, claims: ClaimsFromWrites) -> None:
        self.client = client
        self.claims = claims
        self.tasks: dict[str, TaskDTO] = {}
        #: The engineering Run each tracked task's latest attempt is.
        self.latest_run: dict[str, str] = {}
        self.owed: list[OwnerNotification] = []
        client.park_task_waiting_resources.side_effect = self._park
        client.resume_task_from_resource_wait.side_effect = self._resume
        client.get_task.side_effect = self._get_task

    def track(self, task: TaskDTO, *, run_id: str | None = None) -> TaskDTO:
        self.tasks[task.id] = task
        if run_id is not None:
            self.latest_run[task.id] = run_id
        return task

    def status(self, task_id: str) -> TaskStatus:
        return self.tasks[task_id].status

    async def _get_task(self, task_id: str) -> TaskDTO:
        return self.tasks[task_id]

    async def _owe(
        self,
        task: TaskDTO,
        run_id: str,
        *,
        event: OwnerNotificationEvent,
        text: str,
        expected: tuple[TaskStatus, ...],
    ) -> OwnerNotification:
        story = await self.client.get_story(task.story_id)
        record = OwnerNotification(
            event=event,
            text=text,
            story_id=task.story_id,
            project_id=str(task.project_id),
            terminal_status=story.status,
            task_id=task.id,
            expected_task_statuses=expected,
            state=OwnerNotificationState.OWED,
            owed_at=datetime.now(UTC),
        )
        self.claims.owe_run(run_id, record)
        self.owed.append(record)
        return record

    async def _park(self, task_id: str, command: TaskResourceWaitCommand) -> TaskResourceWaitRead:
        task = self.tasks[task_id]
        self.latest_run[task_id] = command.run_id
        if task.status is TaskStatus.WAITING_RESOURCES:
            return TaskResourceWaitRead(
                disposition=TaskResourceWaitDisposition.ALREADY_WAITING,
                task_id=task_id,
                run_id=command.run_id,
                task_status=task.status,
                new_wait=False,
            )
        metadata = dict(task.failure_metadata or {})
        new_wait = "resource_wait_started_at" not in metadata
        metadata.setdefault("resource_wait_started_at", datetime.now(UTC).isoformat())
        metadata.update(
            {
                "allocation_required_ram_mb": command.allocation_required_ram_mb,
                "allocation_min_disk_mb": command.allocation_min_disk_mb,
                "allocation_failure_reason": command.allocation_failure_reason.value,
            }
        )
        task.failure_metadata = metadata
        task.status = TaskStatus.WAITING_RESOURCES
        record = None
        if new_wait:
            record = await self._owe(
                task,
                command.run_id,
                event=command.event,
                text=command.text,
                expected=RESOURCE_WAIT_TASK_STATUSES,
            )
        return TaskResourceWaitRead(
            disposition=TaskResourceWaitDisposition.PARKED,
            task_id=task_id,
            run_id=command.run_id,
            task_status=task.status,
            new_wait=new_wait,
            owner_notification=record,
        )

    async def _resume(
        self, task_id: str, command: TaskResourceResumeCommand
    ) -> TaskResourceResumeRead:
        task = self.tasks[task_id]
        if task.status is not TaskStatus.WAITING_RESOURCES:
            return TaskResourceResumeRead(
                disposition=TaskResourceResumeDisposition.NOT_WAITING,
                task_id=task_id,
                task_status=task.status,
            )
        run_id = self.latest_run[task_id]
        task.status = TaskStatus.TODO
        record = await self._owe(
            task,
            run_id,
            event=OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
            text=command.text,
            expected=RESOURCES_RESUMED_TASK_STATUSES,
        )
        return TaskResourceResumeRead(
            disposition=TaskResourceResumeDisposition.RESUMED,
            task_id=task_id,
            task_status=task.status,
            run_id=run_id,
            owner_notification=record,
        )
