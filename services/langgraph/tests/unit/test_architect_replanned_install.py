"""A catalog story is planned against the catalog, or not planned yet.

After an operator `replan` cancelled a story's install Task, the Architect plans that
install again itself, against the catalog it reads now, with no LLM and no other task.
A catalog story whose catalog cannot be read is not planned at all: the failure is
retriable, and the retry the API schedules plans it once the catalog answers.

Everything past the API boundary is real: the consumer, `plan_install` and its payload
resolution over the pinned kit's catalog (`kit_catalog_off_github`), coverage and the
planning record the API would write (`failed_record`). The graph is a mock that must
never be built on these paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.catalog_install import CatalogInstall, InstallOperation
from shared.contracts.dto.product_brief import (
    ProductBriefContent,
    ProductBriefPlanningAttemptOutcome,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_planning import (
    StoryPlanningOutcome,
    StoryPlanningState,
    failed_record,
    planned_record,
)
from shared.contracts.dto.task import TaskDTO, TaskType
from shared.contracts.queues.architect import ArchitectMessage
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable
from tests.unit.factories import (
    make_planning_attempt,
    make_product_brief,
    make_project,
    make_repository,
    make_story,
    make_task,
    make_task_event,
)
from tests.unit.test_architect_consumer import _FakeBriefBoundary, _FakeRedis

STORY_ID = "story-replan"
OLD_INSTALL = "task-install-old"
UNAVAILABLE = KitCatalogUnavailable(
    "https://kit.invalid/catalog.yaml",
    KitCatalogFailure.TRANSPORT,
    "ConnectError: All connection attempts failed",
)


def _install(name: str, version: str) -> CatalogInstall:
    return CatalogInstall(
        package={
            "name": name,
            "distribution": f"codegen-kit-{name}",
            "version": version,
            "tag": f"packages/{name}/v{version}",
        },
        libraries=[],
        binding={
            "package": name,
            "resource": f"{name.replace('-', '_')}.bindings:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": [],
        },
        core_version="0.6.2",
        python_version="3.12.0",
        catalog_digest="b" * 64,
        tooling_commit="c" * 40,
    )


def _replan_note(noted_at: datetime, task_id: str = OLD_INSTALL):
    operation = InstallOperation(
        id="install-old",
        project_id=make_project().id,
        task_id=task_id,
        story_id=STORY_ID,
        repository_id="repo-1",
        cycle_started_at=noted_at - timedelta(days=1),
        state="published",
        stage="published",
    )
    return make_task_event(
        id=7,
        task_id=task_id,
        event_type="note",
        actor="user:7",
        details={
            "catalog_install_settlement": operation.model_dump(mode="json"),
            "operator_action": "replan",
        },
        created_at=noted_at,
    )


class _ReplanApi(_FakeBriefBoundary):
    """The story, its task history and the brief boundary, as the API would answer."""

    def __init__(self, *, status=StoryStatus.REOPENED, brief=None, claim=None):
        super().__init__(brief)
        reopened = datetime.now(UTC) - timedelta(minutes=5)
        self.story = make_story(
            id=STORY_ID,
            status=status,
            created_at=reopened - timedelta(days=1),
            reopened_at=reopened,
        )
        self.old_install = make_task(
            id=OLD_INSTALL,
            type=TaskType.INSTALL,
            status="cancelled",
            story_id=STORY_ID,
            repository_id="repo-1",
            install=_install("reminders", "0.4.0"),
            created_at=self.story.created_at,
        )
        self.history: list[TaskDTO] = [self.old_install]
        # The replan's transaction opened just before it stamped `reopened_at`.
        self.events = {OLD_INSTALL: [_replan_note(reopened - timedelta(milliseconds=400))]}
        self.claim = claim
        self.created: list[dict] = []
        self.transitions: list[str] = []

    async def get_story(self, story_id):
        return self.story

    async def get_project(self, project_id, **_kwargs):
        return make_project(config={"modules": ["backend", "tg_bot"]})

    async def get_primary_repository(self, project_id):
        return make_repository()

    async def get_tasks_by_story(self, story_id):
        return list(self.history)

    async def get_task_events(self, task_id):
        return self.events.get(task_id, [])

    async def transition_story(self, story_id, action):
        self.transitions.append(action)
        self.story = self.story.model_copy(update={"status": StoryStatus.IN_PROGRESS})
        return self.story

    async def claim_planning_attempt(self, brief_id):
        if self.claim is ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED:
            return make_planning_attempt(outcome=self.claim, planning_attempt_id="plan-old")
        return await super().claim_planning_attempt(brief_id)

    async def create_task(self, task_data):
        await super().create_task(task_data)
        self.created.append(task_data)
        task = make_task(
            id=f"task-new-{len(self.created)}",
            type=task_data["type"],
            status=task_data["status"],
            story_id=task_data["story_id"],
            repository_id=task_data["repository_id"],
            install=task_data["install"],
            planning_attempt_id=task_data.get("planning_attempt_id"),
        )
        self.tasks[task.id] = self.tasks.pop(f"task-{len(self.tasks)}")
        self.history.append(task)
        return task

    async def record_planning_outcome(self, story_id, report):
        """What `POST /stories/{id}/planning-outcome` writes on the story."""
        self.planning_reports.append(report)
        now = datetime.now(UTC)
        if report.outcome is StoryPlanningOutcome.SUCCEEDED:
            planning = planned_record(
                report,
                planning_attempt_id=report.planning_attempt_id,
                reopen=report.reopen,
                now=now,
            )
        else:
            planning = failed_record(self.story.planning, report, max_retries=3, now=now)
        self.story = self.story.model_copy(update={"planning": planning})
        return planning


@pytest.fixture
def llm_settings():
    settings = MagicMock(
        architect_llm_api_key="test-key",
        architect_llm_model="test-model",
        architect_llm_base_url="http://test",
    )
    with patch("src.consumers.architect.get_settings", return_value=settings):
        yield


async def _run(api: _ReplanApi, *, is_reopen: bool = True, user_report: str | None = None):
    """One architect job on the story; returns the result and the graph factory."""
    from src.consumers.architect import process_architect_job

    message = ArchitectMessage(
        story_id=STORY_ID,
        project_id=str(make_project().id),
        telegram_chat_id="4242",
        is_reopen=is_reopen,
        user_report=user_report,
    ).model_dump(mode="json")
    with (
        patch("src.consumers.architect.api_client", api),
        patch("src.agents.architect.tools.api_client", api),
        patch("src.consumers.architect.create_architect_graph") as graph,
    ):
        graph.return_value.ainvoke = AsyncMock(return_value={"messages": []})
        result = await process_architect_job(message, _FakeRedis())
    return result, graph


def _current_release(catalog, name: str) -> str:
    return next(item.version.version for item in catalog.packages if item.name == name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [StoryStatus.REOPENED, StoryStatus.IN_PROGRESS],
    ids=["replan-message", "send-to-architect-resend"],
)
async def test_a_replanned_install_is_planned_again_without_the_llm(
    llm_settings, bundled_kit_catalog, status
):
    api = _ReplanApi(status=status)

    result, graph = await _run(api)

    assert result["status"] == "success", result
    assert result["replanned_install"] == ["reminders"]
    graph.assert_not_called()
    (task,) = api.created
    assert task["type"] == TaskType.INSTALL
    package = task["install"]["package"]
    assert package["name"] == "reminders"
    # The release the catalog lists now, not the one the cancelled Task pinned.
    assert package["version"] == _current_release(bundled_kit_catalog, "reminders") != "0.4.0"
    assert task["planning_attempt_id"] is None
    assert api.transitions == (["start"] if status is StoryStatus.REOPENED else [])
    (report,) = api.planning_reports
    assert report.outcome is StoryPlanningOutcome.SUCCEEDED and report.reopen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(
            lambda api: api.events.update(
                {OLD_INSTALL: [_replan_note(api.story.reopened_at - timedelta(days=2))]}
            ),
            id="reopened-since-the-replan",
        ),
        pytest.param(lambda api: api.events.clear(), id="cancelled-without-a-replan"),
    ],
)
async def test_any_other_reopen_of_an_install_story_is_planned_by_the_llm(llm_settings, arrange):
    api = _ReplanApi()
    arrange(api)

    result, graph = await _run(api, user_report="The reminder never arrives")

    assert result["status"] == "success", result
    graph.assert_called_once()
    prompt = graph.return_value.ainvoke.call_args.args[0]["messages"][0]["content"]
    assert "REOPEN of story story-replan" in prompt
    assert "Kit package catalog (read live from" in prompt


@pytest.mark.asyncio
async def test_a_package_the_catalog_no_longer_installs_parks_the_story_without_a_task(
    llm_settings,
):
    api = _ReplanApi()
    api.old_install = api.old_install.model_copy(
        update={"install": _install("retired-widget", "1.0.0")}
    )
    api.history = [api.old_install]

    result, graph = await _run(api)

    assert result["status"] == "failed"
    assert "CatalogInstallRefused: retired-widget: unknown_package" in result["error"]
    graph.assert_not_called()
    assert api.created == []
    (report,) = api.planning_reports
    assert report.outcome is StoryPlanningOutcome.FAILED and not report.retriable
    assert "unknown_package" in report.failure.detail
    assert api.story.planning.state is StoryPlanningState.PARKED


@pytest.mark.asyncio
async def test_without_the_catalog_a_replan_waits_and_the_scheduled_retry_plans_it(
    llm_settings, kit_catalog_off_github, bundled_kit_catalog
):
    """The production incident, end to end: a restart's transport failure delays the plan."""
    api = _ReplanApi()
    kit_catalog_off_github.read.return_value = UNAVAILABLE

    first, graph = await _run(api)

    assert first["status"] == "failed" and first["planning"] == "retrying"
    graph.assert_not_called()
    assert api.created == []
    assert api.transitions == []
    (report,) = api.planning_reports
    assert report.outcome is StoryPlanningOutcome.FAILED
    assert report.retriable and report.reopen
    assert "KitCatalogUnavailable" in report.failure.detail
    assert "ConnectError: All connection attempts failed" in report.failure.detail
    planning = api.story.planning
    assert planning.state is StoryPlanningState.RETRYING and planning.reopen
    assert api.story.status is StoryStatus.REOPENED

    # A redelivery before the backoff is over plans nothing and spends no retry.
    early, _ = await _run(api)
    assert (early["status"], early["reason"]) == ("skipped", "planning retry not due")
    assert len(api.planning_reports) == 1

    # The backoff is over: the scheduler publishes the due record as the reopen it was.
    due = datetime.now(UTC) - timedelta(seconds=1)
    api.story = api.story.model_copy(
        update={"planning": planning.model_copy(update={"next_attempt_at": due})}
    )
    kit_catalog_off_github.read.return_value = bundled_kit_catalog

    retried, graph = await _run(api, is_reopen=api.story.planning.reopen)

    assert retried["status"] == "success", retried
    graph.assert_not_called()
    (task,) = api.created
    assert task["install"]["package"]["name"] == "reminders"
    assert api.transitions == ["start"]
    assert api.story.planning.state is StoryPlanningState.PLANNED


@pytest.mark.asyncio
async def test_any_story_with_an_install_in_its_history_waits_for_the_catalog(
    llm_settings, kit_catalog_off_github
):
    """Not only a replan: an install story's ordinary reopen is not planned blind either."""
    api = _ReplanApi()
    api.events.clear()
    api.history = [api.old_install.model_copy(update={"status": "done"})]
    kit_catalog_off_github.read.return_value = UNAVAILABLE

    result, graph = await _run(api, user_report="The reminder never arrives")

    assert result["status"] == "failed"
    graph.assert_not_called()
    assert api.created == []
    (report,) = api.planning_reports
    assert report.retriable and "KitCatalogUnavailable" in report.failure.detail


@pytest.mark.asyncio
async def test_a_claimed_brief_waits_for_the_catalog_with_its_attempt_released(
    llm_settings, kit_catalog_off_github
):
    api = _ReplanApi(brief=make_product_brief(story_id=STORY_ID))
    kit_catalog_off_github.read.return_value = UNAVAILABLE

    result, graph = await _run(api)

    assert result["status"] == "failed"
    graph.assert_not_called()
    assert not api.attempt_active
    (report,) = api.planning_reports
    assert report.retriable and report.planning_attempt_id == "plan-live"


@pytest.mark.asyncio
async def test_a_replanned_install_under_a_claimed_brief_covers_and_admits_it(llm_settings):
    brief = make_product_brief(
        story_id=STORY_ID,
        content=ProductBriefContent(
            summary="Reminders",
            must_requirements=[
                {"id": "remind", "text": "Sends a one-time reminder"},
                {"id": "list", "text": "Lists pending reminders"},
            ],
        ),
    )
    api = _ReplanApi(brief=brief)

    result, graph = await _run(api)

    assert result["status"] == "success", result
    graph.assert_not_called()
    (task,) = api.created
    assert task["planning_attempt_id"] == "plan-live"
    assert {rid: covered[1] for rid, covered in api.coverage.items()} == {
        "remind": "task-new-1",
        "list": "task-new-1",
    }
    assert api.admit_calls == 1 and api.released == ["task-new-1"]
    assert api.transitions == ["start"]


@pytest.mark.asyncio
async def test_a_replanned_install_under_an_admitted_brief_owes_no_coverage(llm_settings):
    """As on production: the brief was admitted by the first plan, so the new task is free."""
    brief = make_product_brief(
        story_id=STORY_ID,
        coverage_admitted_at=datetime.now(UTC) - timedelta(days=1),
        planning_attempt_id="plan-old",
    )
    api = _ReplanApi(brief=brief, claim=ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED)

    result, graph = await _run(api)

    assert result["status"] == "success", result
    graph.assert_not_called()
    (task,) = api.created
    assert task["planning_attempt_id"] is None
    assert api.tasks["task-new-1"]["dispatch_admitted"]
    assert api.coverage == {} and api.admit_calls == 0
