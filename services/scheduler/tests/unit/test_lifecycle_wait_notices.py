"""A task's resource-wait announcements are owed, not hoped for.

Parking a refused engineering task and resuming it used to publish their owner
notices straight to ``po:input`` behind a swallowed exception, after the
transition had committed. A transient Redis or recipient failure there lost the
message for good, because nothing scans for a wait whose announcement is
missing. The park and the resume are now API actions that write the owed
record on the refused Run in the move's own transaction; the routing tick then
spends one attempt through the owner-notification seam and the recovery sweep
owns the rest.

These tests drive the real supervisors and the real seam against a stateful
double of those actions (``ResourceWaitDouble``) and of the claim
(``ClaimsFromWrites``), and count what reached ``po:input``. The same flow runs
against the real API and Postgres in
``services/scheduler/tests/service/test_lifecycle_notice_recovery.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _lifecycle_wait_double import ResourceWaitDouble
from _owner_notification_claims import ClaimsFromWrites
from _run_routing_factories import _make_story, _make_task
import pytest

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.run_result import AllocationFailureReason, EngineeringRunResult
from shared.contracts.dto.task import TaskStatus
from shared.queues import PO_INPUT_QUEUE
from shared.tests.server_admission_cases import ADMISSION_CASES, admission_case_server

RUN_ID = "eng-refused-1"


class _PoInput:
    """``po:input``: refuses the next ``failures`` publishes, keeps the rest."""

    def __init__(self) -> None:
        self.failures = 0
        self.published: list[dict] = []

    async def publish_flat(self, queue: str, fields: dict) -> None:
        assert queue == PO_INPUT_QUEUE
        if self.failures:
            self.failures -= 1
            raise ConnectionError("po:input is unreachable")
        self.published.append(fields)

    def events(self) -> list[str]:
        return [fields["event"] for fields in self.published]


class _World:
    """One story task, its refused engineering Run, and the owner who reads about it."""

    def __init__(self) -> None:
        api = AsyncMock()
        api.get_story.return_value = _make_story(id="story-1", status="in_progress")
        api.get_project.return_value = SimpleNamespace(owner_id=42)
        api.get_user.return_value = SimpleNamespace(telegram_id=900000042)
        self.claims = ClaimsFromWrites(api)
        self.waits = ResourceWaitDouble(api, self.claims)
        self.task = self.waits.track(
            _make_task(id="task-1", story_id="story-1", status="failed", current_iteration=1)
        )
        self.run = SimpleNamespace(
            id=RUN_ID,
            run_metadata={"iteration": 1},
            result=EngineeringRunResult(
                engineering_status=EngineeringStatus.FAILED,
                allocation_failure_reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
                allocation_required_ram_mb=768,
                allocation_min_disk_mb=1024,
            ),
        )
        api.get_tasks_by_status.side_effect = self._tasks_by_status
        api.list_runs.return_value = [self.run]
        api.list_runs_owing_owner_notification.side_effect = self._owing
        api.list_stories_owing_owner_notification.return_value = []
        case = next(candidate for candidate in ADMISSION_CASES if candidate.admitted)
        self.servers = [admission_case_server(case, last_health_check=datetime.now(UTC))]
        api.get_servers.return_value = []
        api.list_active_incidents.return_value = []
        api.get_applications.return_value = []
        self.api = api
        self.po = _PoInput()

    async def _tasks_by_status(self, status):
        return [self.task] if self.task.status == status else []

    def record(self) -> OwnerNotification:
        return OwnerNotification.model_validate(self.claims._current("run", RUN_ID))

    async def _owing(self, *, limit):
        stored = self.claims._current("run", RUN_ID)
        if stored is None or not OwnerNotification.model_validate(stored).owed:
            return []
        return [SimpleNamespace(id=RUN_ID, run_metadata={OWNER_NOTIFICATION_KEY: stored})]

    async def park(self) -> None:
        from src.tasks.supervisor import supervise_failed_tasks

        await supervise_failed_tasks(self.api, self.po)

    async def resume(self) -> None:
        from src.tasks.supervisor import supervise_waiting_resource_tasks

        self.api.get_servers.return_value = self.servers
        await supervise_waiting_resource_tasks(self.api, self.po)

    async def later_sweep(self) -> dict[str, int]:
        """The owner-notification loop, at least one attempt interval later."""
        from src.tasks.owner_notifications import supervise_owed_owner_notifications

        self.claims.clock.elapse()
        return await supervise_owed_owner_notifications(self.api, self.po)


@pytest.fixture(autouse=True)
def _no_admin_telegram():
    with patch("src.tasks._recipients.notify_admins_best_effort", new_callable=AsyncMock):
        yield


@pytest.mark.asyncio
async def test_a_redis_failure_after_the_park_leaves_the_wait_owed_and_a_later_sweep_tells_once():
    world = _World()
    world.po.failures = 1

    await world.park()

    assert world.task.status is TaskStatus.WAITING_RESOURCES
    record = world.record()
    assert record.event == "task_waiting_resources"
    assert record.state is OwnerNotificationState.OWED
    assert record.attempts == 1
    assert world.po.published == []

    await world.later_sweep()
    await world.later_sweep()

    assert world.po.events() == ["task_waiting_resources"]
    assert world.po.published[0]["task_id"] == "task-1"
    assert world.record().state is OwnerNotificationState.DELIVERED


@pytest.mark.asyncio
async def test_a_recipient_lookup_failure_after_the_park_is_recovered_by_the_sweep():
    world = _World()
    world.api.get_user.side_effect = [ConnectionError("users API timed out")]

    await world.park()
    assert world.record().state is OwnerNotificationState.OWED
    assert world.po.published == []

    world.api.get_user.side_effect = None
    await world.later_sweep()

    assert world.po.events() == ["task_waiting_resources"]
    assert world.record().state is OwnerNotificationState.DELIVERED


@pytest.mark.asyncio
async def test_a_resume_supersedes_an_undelivered_wait_and_only_the_resume_is_told():
    """The "waiting" message is never published once the task has resumed."""
    world = _World()
    world.po.failures = 1
    await world.park()
    waiting = world.record()
    assert waiting.owed

    await world.resume()

    assert world.task.status is TaskStatus.TODO
    await world.later_sweep()
    assert world.po.events() == ["task_resources_resumed"]
    resumed = world.record()
    assert resumed.event == "task_resources_resumed"
    assert resumed.owed_at > waiting.owed_at
    assert resumed.state is OwnerNotificationState.DELIVERED


@pytest.mark.asyncio
async def test_a_wait_record_whose_task_left_the_wait_is_voided_without_publishing():
    """A task that moved on without a resume (an operator, the wait timing out)."""
    world = _World()
    world.po.failures = 1
    await world.park()

    world.task.status = TaskStatus.WAITING_HUMAN_REVIEW
    await world.later_sweep()

    record = world.record()
    assert record.state is OwnerNotificationState.VOIDED
    assert record.attempts == 1  # the failed publish; the void spent nothing
    assert "waiting_human_review" in record.detail
    assert world.po.published == []


@pytest.mark.asyncio
async def test_a_resumed_record_is_voided_if_the_task_went_back_to_waiting():
    world = _World()
    await world.park()
    world.po.failures = 1
    await world.resume()
    assert world.record().owed

    world.task.status = TaskStatus.WAITING_RESOURCES
    await world.later_sweep()

    assert world.record().state is OwnerNotificationState.VOIDED
    assert world.po.events() == ["task_waiting_resources"]


@pytest.mark.asyncio
async def test_a_task_read_that_fails_spends_an_attempt_instead_of_deciding():
    world = _World()
    world.po.failures = 1
    await world.park()
    world.api.get_task.side_effect = ConnectionError("API restarting")

    await world.later_sweep()

    record = world.record()
    assert record.state is OwnerNotificationState.OWED
    assert record.attempts == 2
    assert world.po.published == []


@pytest.mark.asyncio
async def test_exhausted_attempts_abandon_the_wait_notice_and_call_a_human_once():
    from src.tasks.owner_notifications import OWNER_NOTIFICATION_MAX_ATTEMPTS

    world = _World()
    world.po.failures = OWNER_NOTIFICATION_MAX_ATTEMPTS + 5
    with patch(
        "src.tasks.owner_notifications.notify_admins_best_effort", new_callable=AsyncMock
    ) as alert:
        await world.park()
        for _ in range(OWNER_NOTIFICATION_MAX_ATTEMPTS + 1):
            await world.later_sweep()

    record = world.record()
    assert record.state is OwnerNotificationState.ABANDONED
    assert record.attempts == OWNER_NOTIFICATION_MAX_ATTEMPTS
    alert.assert_awaited_once()
    assert f"run={RUN_ID}" in alert.await_args.args[0]
    assert world.po.published == []


@pytest.mark.asyncio
async def test_an_unaddressable_owner_settles_the_wait_notice_without_retrying():
    world = _World()
    world.api.get_user.return_value = SimpleNamespace(telegram_id=None)

    await world.park()
    await world.later_sweep()

    record = world.record()
    assert record.state is OwnerNotificationState.UNADDRESSABLE
    assert record.attempts == 1
    assert world.po.published == []


@pytest.mark.asyncio
async def test_a_failed_in_tick_claim_does_not_end_the_tick_or_lose_the_notice():
    """The move committed with its record; the tick's attempt is only an optimisation."""
    world = _World()
    world.api.claim_run_owner_notification_attempt.side_effect = [ConnectionError("API restarting")]

    await world.park()

    assert world.task.status is TaskStatus.WAITING_RESOURCES
    assert world.record().owed
    world.api.claim_run_owner_notification_attempt.side_effect = world.claims._claim_run
    await world.later_sweep()
    assert world.po.events() == ["task_waiting_resources"]


def _move_while_the_recipient_is_resolved(world: _World, status: TaskStatus) -> None:
    """The owner lookup stalls, and in that time a move commits the task elsewhere."""
    owner = world.api.get_user.return_value

    async def lookup(user_id):
        world.task.status = status
        return owner

    world.api.get_user.side_effect = lookup


@pytest.mark.asyncio
async def test_a_resume_committing_while_the_wait_notice_is_addressed_is_not_announced_as_waiting():
    """The check is the last read before the publish, not only the first one."""
    world = _World()
    world.po.failures = 1
    await world.park()
    assert world.record().owed

    _move_while_the_recipient_is_resolved(world, TaskStatus.TODO)
    await world.later_sweep()

    world.api.get_user.assert_awaited()
    record = world.record()
    assert record.state is OwnerNotificationState.VOIDED
    assert "task is todo" in record.detail
    assert record.attempts == 1  # the failed publish; the void spent nothing
    assert world.po.published == []


@pytest.mark.asyncio
async def test_a_task_leaving_todo_while_the_resume_notice_is_addressed_is_not_announced():
    world = _World()
    await world.park()
    world.po.failures = 1
    await world.resume()
    assert world.record().event == "task_resources_resumed"
    assert world.record().owed

    _move_while_the_recipient_is_resolved(world, TaskStatus.WAITING_HUMAN_REVIEW)
    await world.later_sweep()

    world.api.get_user.assert_awaited()
    record = world.record()
    assert record.state is OwnerNotificationState.VOIDED
    assert "task is waiting_human_review" in record.detail
    assert world.po.events() == ["task_waiting_resources"]


@pytest.mark.asyncio
async def test_a_failed_final_read_spends_an_attempt_and_publishes_nothing():
    world = _World()
    world.po.failures = 1
    await world.park()
    reads = [world.task, ConnectionError("API restarting")]

    async def get_task(task_id):
        answer = reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    world.api.get_task.side_effect = get_task
    await world.later_sweep()

    record = world.record()
    assert record.state is OwnerNotificationState.OWED
    assert record.attempts == 2
    assert "API restarting" in record.detail
    assert world.po.published == []
