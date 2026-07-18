"""Tests for scaffold trigger in scheduler."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus, ServiceModule
from shared.contracts.dto.repository import RepositoryDTO
from src.tasks.scaffold_trigger import _template_config, trigger_scaffolds

# Stable UUIDs for tests
PROJ_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_REPO = RepositoryDTO(
    id="repo-1",
    project_id=PROJ_UUID,
    name="test-project",
    git_url="https://github.com/org/test-project",
    role="primary",
    visibility="private",
    is_managed=True,
    created_at=datetime.now(UTC),
)


def _make_project(
    status: str,
    modules: list[str] | None = None,
    project_id: uuid.UUID | None = None,
    config: dict | None = None,
) -> ProjectDTO:
    return ProjectDTO(
        id=project_id or PROJ_UUID,
        title="test-project",
        slug="test-project-0000",
        status=ProjectStatus(status),
        modules=[ServiceModule(m) for m in (modules or ["backend"])],
        owner_id=1,
        config=config or {},
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.get_projects.return_value = []
    api.get_stories_by_project.return_value = []
    api.get_repositories.return_value = []
    api.get_tasks_by_project_and_status.return_value = []
    return api


@pytest.fixture(autouse=True)
def template_config(monkeypatch):
    config = MagicMock()
    config.get.side_effect = lambda key: {
        "scheduler.service_template_source": "gh:vladmesh/service-template",
        "scheduler.service_template_ref": "0.3.0",
    }[key]
    monkeypatch.setattr("src.tasks.scaffold_trigger.startup.config", config)


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.publish_message = AsyncMock()
    return redis


class TestTriggerScaffolds:
    def test_template_config_reads_initialized_startup_store(self, template_config):
        assert _template_config() == ("gh:vladmesh/service-template", "0.3.0")

    @pytest.mark.asyncio
    async def test_draft_project_with_stories_publishes_full(self, mock_api, mock_redis):
        project = _make_project(ProjectStatus.DRAFT.value, ["backend", "tg_bot"])
        mock_api.get_projects.return_value = [project]
        mock_api.get_stories_by_project.return_value = [{"id": "story-1"}]
        mock_api.get_repositories.return_value = [_REPO]

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 1
        mock_redis.publish_message.assert_called_once()
        msg = mock_redis.publish_message.call_args[0][1]
        assert msg.mode == "full"
        assert msg.template_repo == "gh:vladmesh/service-template"
        assert msg.template_ref == "0.3.0"

    @pytest.mark.asyncio
    async def test_active_project_with_todo_tasks_publishes_ensure(
        self,
        mock_api,
        mock_redis,
    ):
        """ACTIVE project with TODO tasks and no workspace_ready → mode=ensure."""
        project = _make_project(ProjectStatus.ACTIVE.value)
        mock_api.get_projects.return_value = [project]
        mock_api.get_tasks_by_project_and_status.return_value = [{"id": "task-1"}]
        mock_api.get_repositories.return_value = [_REPO]

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 1
        msg = mock_redis.publish_message.call_args[0][1]
        assert msg.mode == "ensure"

    @pytest.mark.asyncio
    async def test_active_project_workspace_ready_is_skipped(self, mock_api, mock_redis):
        """ACTIVE project with workspace_ready=true → skip."""
        project = _make_project(
            ProjectStatus.ACTIVE.value,
            config={"workspace_ready": True},
        )
        mock_api.get_projects.return_value = [project]

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 0
        mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_project_no_todo_tasks_is_skipped(self, mock_api, mock_redis):
        """ACTIVE project with no TODO tasks → skip."""
        project = _make_project(ProjectStatus.ACTIVE.value)
        mock_api.get_projects.return_value = [project]
        mock_api.get_tasks_by_project_and_status.return_value = []

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 0
        mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_archived_project_is_skipped(self, mock_api, mock_redis):
        project = _make_project(ProjectStatus.ARCHIVED.value)
        mock_api.get_projects.return_value = [project]

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 0
        mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_project_without_stories_is_skipped(self, mock_api, mock_redis):
        project = _make_project(ProjectStatus.DRAFT.value)
        mock_api.get_projects.return_value = [project]
        mock_api.get_stories_by_project.return_value = []

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 0
        mock_redis.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_project_without_repo_is_skipped(self, mock_api, mock_redis):
        project = _make_project(ProjectStatus.DRAFT.value)
        mock_api.get_projects.return_value = [project]
        mock_api.get_stories_by_project.return_value = [{"id": "story-1"}]
        mock_api.get_repositories.return_value = []

        count = await trigger_scaffolds(mock_api, mock_redis)

        assert count == 0
        mock_redis.publish_message.assert_not_called()
