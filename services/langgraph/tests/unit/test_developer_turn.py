"""What a developer turn on a story worker is told, and whether it starts fresh.

The 2026-09-26 case (mega-live 36234323365, task-c4f82b78): the story's second
task went to the worker that had just finished the first, resumed that
conversation, and reported the first task's commit after 20 seconds. A reused
worker handed a different task, or a retry after an attempt that changed
nothing, starts a fresh session with TASK.md naming the task; a retry of the
same task after any other failure keeps its session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.worker_turn import AttemptTurnMetadata
from src.clients.worker_spawner import SpawnResult
from tests.unit.factories import make_repository, make_run, make_task
from tests.unit.test_developer_node import _make_state

WORKER = "dev-stand-t-76807b278121-642444af"
TASK_1 = "task-backend"
TASK_2 = "task-c4f82b78"
TASK_2_TITLE = "tg_bot: /level1 command, command menu, location handler"
NEW_TASK_HEADING = "## This turn's task"
NO_CHANGES_HEADING = "## The previous attempt made no changes"


def _attempt(
    run_id: str,
    task_id: str | None,
    *,
    worker_id: str = WORKER,
    failed: bool = False,
    failure_reason: EngineeringFailureReason | None = None,
):
    """An earlier engineering attempt of the story, as the Run list returns it."""
    failed = failed or failure_reason is not None
    return make_run(
        id=run_id,
        type=RunType.ENGINEERING,
        status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
        story_id="story-1",
        task_id=task_id,
        run_metadata=AttemptTurnMetadata(worker_id=worker_id).as_run_metadata(),
        result=EngineeringRunResult(
            engineering_status=EngineeringStatus.FAILED if failed else EngineeringStatus.DONE,
            failure_reason=failure_reason,
        ),
    )


def _state(*, task_id: str | None = TASK_2, worker_id: str | None = WORKER) -> dict:
    state = _make_state(action="feature", status="active")
    state["description"] = "Add the /level1 command"
    state["story_id"] = "story-1"
    state["planning_task_id"] = task_id
    state["worker_id"] = worker_id
    return state


async def _turn(state: dict, earlier: list, *, reused: bool = True):
    """Run the developer node and return how the worker was asked for the turn."""
    from src.nodes.developer import DeveloperNode

    api = AsyncMock()
    api.get_project = AsyncMock(return_value=None)
    api.get_primary_repository = AsyncMock(return_value=make_repository())
    api.list_story_engineering_runs = AsyncMock(
        return_value=[make_run(id="eng-1", type=RunType.ENGINEERING, task_id=TASK_2), *earlier]
    )
    api.get_task = AsyncMock(return_value=make_task(id=TASK_2, title=TASK_2_TITLE))
    github = AsyncMock()
    github.get_repo_scoped_token = AsyncMock(return_value="ghs_fake")
    worker = AsyncMock(
        return_value=SpawnResult(
            request_id="req-1", success=True, exit_code=0, output="Done", commit_sha="abc123"
        )
    )
    target = "send_task_to_worker" if reused else "request_spawn"
    with (
        patch("src.nodes.developer.api_client", api),
        patch("src.nodes.developer.GitHubAppClient", return_value=github),
        patch(f"src.nodes.developer.{target}", worker),
    ):
        await DeveloperNode().run(state)
    return worker.await_args.kwargs


class TestDifferentTaskOnAReusedWorker:
    @pytest.mark.asyncio
    async def test_starts_a_fresh_session_named_for_the_new_task(self):
        sent = await _turn(_state(), [_attempt("eng-0", TASK_1)])

        assert sent["clear_session"] is True
        content = sent["task_content"]
        assert content.startswith(NEW_TASK_HEADING)
        assert f"`{TASK_2}`: {TASK_2_TITLE}" in content
        assert "earlier tasks in this story are" in content
        assert "not this task's result" in content
        assert NO_CHANGES_HEADING not in content

    @pytest.mark.asyncio
    async def test_a_worker_whose_last_turn_is_not_on_record_starts_fresh(self):
        """No Run names this worker: its last task is unknown, so it is not this one."""
        sent = await _turn(_state(), [_attempt("eng-0", TASK_1, worker_id="another-worker")])

        assert sent["clear_session"] is True
        assert sent["task_content"].startswith(NEW_TASK_HEADING)


class TestSameTaskRetryOnAReusedWorker:
    @pytest.mark.asyncio
    async def test_an_ordinary_failure_keeps_the_session_and_the_prompt(self):
        """Today's behaviour: the retry resumes the turn that worked on this task."""
        earlier = [
            _attempt("eng-0b", TASK_2, failed=True),
            _attempt("eng-0", TASK_1),
        ]

        sent = await _turn(_state(), earlier)

        assert sent["clear_session"] is False
        assert NEW_TASK_HEADING not in sent["task_content"]
        assert NO_CHANGES_HEADING not in sent["task_content"]

    @pytest.mark.asyncio
    async def test_a_retry_after_no_changes_starts_fresh_and_says_why(self):
        earlier = [
            _attempt("eng-0b", TASK_2, failure_reason=EngineeringFailureReason.NO_NEW_COMMIT),
            _attempt("eng-0", TASK_1),
        ]

        sent = await _turn(_state(), earlier)

        assert sent["clear_session"] is True
        content = sent["task_content"]
        assert content.startswith(NO_CHANGES_HEADING)
        assert f"`{TASK_2}` ({TASK_2_TITLE})" in content
        assert "without changing" in content
        assert NEW_TASK_HEADING not in content


class TestTurnsWithoutAReusedWorker:
    @pytest.mark.asyncio
    async def test_a_fresh_worker_after_no_changes_is_still_told_why(self):
        earlier = [
            _attempt("eng-0b", TASK_2, failure_reason=EngineeringFailureReason.NO_NEW_COMMIT),
        ]

        sent = await _turn(_state(worker_id=None), earlier, reused=False)

        assert "clear_session" not in sent
        assert sent["task_content"].startswith(NO_CHANGES_HEADING)

    @pytest.mark.asyncio
    async def test_a_taskless_repair_keeps_todays_turn(self):
        """A deploy repair has no task identity, so nothing is compared or said."""
        sent = await _turn(_state(task_id=None), [_attempt("eng-0", TASK_1)])

        assert sent["clear_session"] is False
        assert not sent["task_content"].startswith("## ")
