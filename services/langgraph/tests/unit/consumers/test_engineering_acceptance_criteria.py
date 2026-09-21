"""The developer is told exactly what QA will check.

Every engineering producer — task dispatcher, API manual start, deploy code fix —
publishes to the same consumer, so these tests drive the consumer with a message
shaped the way each producer builds it, run the real developer node inside the
subgraph, and read the TASK.md it hands to the worker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.contracts.acceptance import BASELINE_ACCEPTANCE_CRITERIA
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.vocab import ActionType
from src.clients.worker_spawner import SpawnResult
from src.nodes.developer_tasks import build_task_message
from tests.unit.factories import make_project, make_repository, make_task

# The 2026-09-17 case (sprint:1445, story-9d65206d): the architect quoted the
# brief's usage-example replies, central QA checked them literally, and the
# worker never saw them.
TASK_CRITERIA = (
    "- /income 5000 зарплата → бот отвечает «Доход 5000 «зарплата» записан.»\n"
    "- /expense 300 кофе → бот отвечает «Расход 300 «кофе» записан.»\n"
    "- /balance → бот отвечает «Текущий баланс: 4700.»"
)
REPOSITORY_CRITERIA = f"{BASELINE_ACCEPTANCE_CRITERIA}\n{TASK_CRITERIA}"
QUOTE = "«Доход 5000 «зарплата» записан.»"

TASK_HEADING = "## Acceptance Criteria (QA checks these literally)"
CHECKLIST_HEADING = "## Post-Deploy QA Checklist"


def _dispatcher_message(**overrides) -> dict:
    """`task_dispatcher`: a planned or QA-fix task, story-scoped, deploy at story level."""
    message = EngineeringMessage(
        task_id="eng-1",
        project_id="proj-1",
        initiating_run_id="live-1",
        action="feature",
        description="Record income and expenses",
        skip_deploy=True,
        planning_task_id="task-62fc50cb",
        story_id="story-9d65206d",
        branch="story/story-9d65206d",
    )
    return message.model_copy(update=overrides).model_dump(mode="json")


def _api_manual_start_message() -> dict:
    """`_task_actions`: a planned task started by hand, no branch."""
    return EngineeringMessage(
        task_id="eng-2",
        project_id="proj-1",
        initiating_run_id="live-1",
        action="feature",
        description="Record income and expenses",
        planning_task_id="task-62fc50cb",
        story_id="story-9d65206d",
    ).model_dump(mode="json")


def _deploy_code_fix_message() -> dict:
    """`supervisor/deploy`: a deploy code fix, with no planning task."""
    return EngineeringMessage(
        task_id="eng-3",
        project_id="proj-1",
        initiating_run_id="live-1",
        action="fix",
        description="Deploy failed: container exits on start",
        skip_deploy=False,
        story_id="story-9d65206d",
        deploy_fix_attempt=1,
    ).model_dump(mode="json")


def _consumer_api(*, task=None, repository=None, task_error=None, repository_error=None):
    api = MagicMock()
    api.patch = AsyncMock()
    api.get_run = AsyncMock(return_value=SimpleNamespace(run_metadata={}))
    api.get_project = AsyncMock(return_value=make_project(config={"modules": ["backend"]}))
    api.get_task = AsyncMock(return_value=task, side_effect=task_error)
    api.get_primary_repository = AsyncMock(return_value=repository, side_effect=repository_error)
    return api


async def _task_md(job_data: dict, api: MagicMock) -> str:
    """Run the consumer with the real developer node and return the worker's TASK.md."""
    from src.consumers.engineering import process_engineering_job
    from src.nodes.developer import DeveloperNode

    spawn = AsyncMock(
        return_value=SpawnResult(request_id="req-1", success=False, exit_code=1, output="stop")
    )

    async def _invoke(state: dict) -> dict:
        await DeveloperNode().run(state)
        return {"engineering_status": "failed", "errors": ["stop"]}

    subgraph = MagicMock()
    subgraph.ainvoke = AsyncMock(side_effect=_invoke)
    developer_api = MagicMock()
    developer_api.get_project = AsyncMock(return_value=None)
    developer_api.get_primary_repository = AsyncMock(return_value=make_repository())
    github = MagicMock()
    github.return_value.get_repo_scoped_token = AsyncMock(return_value="ghs_fake")

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.subgraphs.engineering.create_engineering_subgraph", return_value=subgraph),
        patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock),
        patch("src.consumers.engineering.get_story_worker", AsyncMock(return_value=None)),
        patch("src.consumers.engineering._build_story_context", AsyncMock(return_value=None)),
        patch("src.consumers.engineering._build_story_md", AsyncMock(return_value=None)),
        patch("src.consumers.engineering._fail_job", new_callable=AsyncMock) as fail_job,
        patch("src.consumers.engineering.resource_allocator_node") as allocator,
        patch("src.nodes.developer.api_client", developer_api),
        patch("src.nodes.developer.GitHubAppClient", github),
        patch("src.nodes.developer.request_spawn", spawn),
    ):
        allocator.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})
        await process_engineering_job(job_data, AsyncMock())

    # The run reached the worker: nothing in the criteria reads failed it early.
    subgraph.ainvoke.assert_awaited_once()
    assert fail_job.await_args.args[1] == "stop"
    return spawn.await_args.kwargs["task_content"]


class TestEveryProducerCarriesTheCriteria:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "job_data",
        [_dispatcher_message(), _api_manual_start_message()],
        ids=["task-dispatcher", "api-manual-start"],
    )
    async def test_planned_task_carries_its_own_and_the_repository_criteria(self, job_data):
        """Regression 2026-09-17: both quotes reach TASK.md verbatim."""
        api = _consumer_api(
            task=make_task(id="task-62fc50cb", acceptance_criteria=TASK_CRITERIA),
            repository=make_repository(acceptance_criteria=REPOSITORY_CRITERIA),
        )

        task_md = await _task_md(job_data, api)

        api.get_task.assert_awaited_once_with("task-62fc50cb")
        task_section = task_md.split(TASK_HEADING, 1)[1].split(CHECKLIST_HEADING, 1)[0]
        checklist_section = task_md.split(CHECKLIST_HEADING, 1)[1]
        assert TASK_CRITERIA in task_section
        assert QUOTE in task_section
        assert REPOSITORY_CRITERIA in checklist_section
        assert QUOTE in checklist_section
        assert "must match exactly" in task_section

    @pytest.mark.asyncio
    async def test_qa_fix_task_carries_the_repository_checklist(self):
        """A QA-fix task has no criteria of its own; the checklist it must pass is still there."""
        api = _consumer_api(
            task=make_task(id="task-fix", type="fix", acceptance_criteria=None),
            repository=make_repository(acceptance_criteria=REPOSITORY_CRITERIA),
        )

        task_md = await _task_md(
            _dispatcher_message(action=ActionType.FIX, planning_task_id="task-fix"), api
        )

        assert TASK_HEADING not in task_md
        assert REPOSITORY_CRITERIA in task_md.split(CHECKLIST_HEADING, 1)[1]

    @pytest.mark.asyncio
    async def test_deploy_code_fix_without_a_planning_task_carries_the_checklist(self):
        api = _consumer_api(repository=make_repository(acceptance_criteria=REPOSITORY_CRITERIA))

        with patch("src.consumers.acceptance_context.logger") as logger:
            task_md = await _task_md(_deploy_code_fix_message(), api)

        api.get_task.assert_not_awaited()
        logger.warning.assert_not_called()
        assert TASK_HEADING not in task_md
        assert QUOTE in task_md.split(CHECKLIST_HEADING, 1)[1]


class TestUnreadableCriteriaNeverFailTheRun:
    @pytest.mark.asyncio
    async def test_failed_repository_read_omits_the_checklist_and_warns_once(self):
        api = _consumer_api(
            task=make_task(id="task-62fc50cb", acceptance_criteria=TASK_CRITERIA),
            repository_error=httpx.ConnectError("api unavailable"),
        )

        with patch("src.consumers.acceptance_context.logger") as logger:
            task_md = await _task_md(_dispatcher_message(), api)

        assert CHECKLIST_HEADING not in task_md
        assert TASK_CRITERIA in task_md
        assert logger.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_repository_omits_the_checklist_and_warns_once(self):
        api = _consumer_api(
            task=make_task(id="task-62fc50cb", acceptance_criteria=TASK_CRITERIA),
            repository=None,
        )

        with patch("src.consumers.acceptance_context.logger") as logger:
            task_md = await _task_md(_dispatcher_message(), api)

        assert CHECKLIST_HEADING not in task_md
        assert logger.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_planning_task_omits_its_section_and_warns_once(self):
        request = httpx.Request("GET", "http://api/tasks/task-62fc50cb")
        not_found = httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request)
        )
        api = _consumer_api(
            task_error=not_found,
            repository=make_repository(acceptance_criteria=REPOSITORY_CRITERIA),
        )

        with patch("src.consumers.acceptance_context.logger") as logger:
            task_md = await _task_md(_dispatcher_message(), api)

        assert TASK_HEADING not in task_md
        assert REPOSITORY_CRITERIA in task_md
        assert logger.warning.call_count == 1


class TestTaskMdRendering:
    @pytest.mark.parametrize("action", ["create", "feature", "fix"])
    def test_every_action_renders_the_task_criteria_verbatim(self, action):
        task_md = build_task_message(
            project_name="finance-bot",
            description="Personal finance bot",
            modules=["backend"],
            repo_full_name="org/finance-bot",
            project_spec={},
            action=action,
            feature_description="Record income",
            task_acceptance_criteria=TASK_CRITERIA,
        )

        assert TASK_HEADING in task_md
        assert TASK_CRITERIA in task_md
        assert CHECKLIST_HEADING not in task_md

    @pytest.mark.parametrize("action", ["create", "feature", "fix"])
    @pytest.mark.parametrize("criteria", [None, "", "  \n"])
    def test_no_criteria_renders_no_empty_section(self, action, criteria):
        task_md = build_task_message(
            project_name="finance-bot",
            description="Personal finance bot",
            modules=["backend"],
            repo_full_name="org/finance-bot",
            project_spec={},
            action=action,
            feature_description="Record income",
            task_acceptance_criteria=criteria,
            repository_acceptance_criteria=criteria,
        )

        assert "Acceptance Criteria" not in task_md
        assert "QA Checklist" not in task_md
