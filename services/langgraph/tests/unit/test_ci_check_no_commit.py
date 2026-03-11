"""Tests for CI-check task: allow success without commit.

CI-check tasks (created_by=system, appended by architect) may complete
successfully without producing a commit — when all tests already pass.
The engineering worker must mark them as done instead of failing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.task import TaskStatus


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


def _make_job(*, planning_task_id=None, story_id=None):
    return {
        "task_id": "eng-1",
        "project_id": "proj-1",
        "user_id": "12345",
        "action": "feature",
        "skip_deploy": True,
        "planning_task_id": planning_task_id,
        "story_id": story_id,
    }


def _setup_api_mock(api, *, created_by="system"):
    """Configure api_client mock with standard responses."""
    api.patch = AsyncMock()
    api.post = AsyncMock()
    api.get = AsyncMock(
        side_effect=lambda path, **kw: {"created_by": created_by} if "tasks/" in path else []
    )
    api.get_project = AsyncMock(
        return_value={
            "id": "proj-1",
            "name": "test",
            "config": {"modules": ["backend"]},
        }
    )
    api.get_primary_repository = AsyncMock(
        return_value={"id": "repo-1", "git_url": "https://github.com/org/test"}
    )
    api.get_project_allocations = AsyncMock(
        return_value=[{"server_handle": "srv-1", "port": 8000, "server_ip": "1.2.3.4"}]
    )
    api.get_tasks_by_story = AsyncMock(return_value=[])


class TestCiCheckNoCommitSuccess:
    """CI-check tasks should succeed without a commit when nothing needs fixing."""

    @pytest.mark.asyncio
    async def test_ci_check_no_commit_returns_done(self, mock_redis):
        """CI-check task (created_by=system) with no commit_sha → status completed."""
        with (
            patch("src.consumers.engineering.api_client") as api,
            patch("src.subgraphs.engineering.create_engineering_subgraph") as factory,
            patch("src.consumers.engineering.resource_allocator_node"),
            patch("src.consumers.engineering.get_story_worker", return_value=None),
            patch("src.consumers.engineering._wait_for_ci_and_fix"),
            patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock),
            patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock),
        ):
            _setup_api_mock(api, created_by="system")

            graph = AsyncMock()
            graph.ainvoke = AsyncMock(
                return_value={
                    "engineering_status": "done",
                    "commit_sha": None,
                    "worker_id": "w-1",
                    "allow_no_commit": True,
                }
            )
            factory.return_value = graph

            from src.consumers.engineering import process_engineering_job

            result = await process_engineering_job(
                _make_job(planning_task_id="task-ci"), mock_redis
            )

            assert result["status"] == RunStatus.COMPLETED

            # Run should be patched to completed
            run_patch_calls = [c for c in api.patch.call_args_list if "runs/" in str(c)]
            assert any(RunStatus.COMPLETED in str(c) for c in run_patch_calls), (
                "Run must be set to completed"
            )

            # Task should be transitioned to done (via api.post transition)
            task_transition_calls = [c for c in api.post.call_args_list if "transition" in str(c)]
            assert any(TaskStatus.DONE in str(c) for c in task_transition_calls), (
                "CI-check task must be transitioned to done"
            )

    @pytest.mark.asyncio
    async def test_regular_task_no_commit_still_fails(self, mock_redis):
        """Regular task (created_by=architect) with no commit_sha → status failed."""
        with (
            patch("src.consumers.engineering.api_client") as api,
            patch("src.subgraphs.engineering.create_engineering_subgraph") as factory,
            patch("src.consumers.engineering.resource_allocator_node"),
            patch("src.consumers.engineering.get_story_worker", return_value=None),
            patch("src.consumers.engineering._wait_for_ci_and_fix"),
            patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock),
            patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock),
        ):
            _setup_api_mock(api, created_by="architect")

            graph = AsyncMock()
            graph.ainvoke = AsyncMock(
                return_value={
                    "engineering_status": "done",
                    "commit_sha": None,
                    "worker_id": "w-1",
                    "allow_no_commit": False,
                }
            )
            factory.return_value = graph

            from src.consumers.engineering import process_engineering_job

            result = await process_engineering_job(
                _make_job(planning_task_id="task-feat"), mock_redis
            )

            assert result["status"] == RunStatus.FAILED

    @pytest.mark.asyncio
    async def test_ci_check_with_commit_still_runs_ci_gate(self, mock_redis):
        """CI-check task that DID produce a commit → normal CI gate flow."""
        with (
            patch("src.consumers.engineering.api_client") as api,
            patch("src.subgraphs.engineering.create_engineering_subgraph") as factory,
            patch("src.consumers.engineering.resource_allocator_node"),
            patch("src.consumers.engineering.get_story_worker", return_value=None),
            patch("src.consumers.engineering._wait_for_ci_and_fix") as ci_gate,
            patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock),
            patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock),
        ):
            _setup_api_mock(api, created_by="system")

            graph = AsyncMock()
            graph.ainvoke = AsyncMock(
                return_value={
                    "engineering_status": "done",
                    "commit_sha": "fix123",
                    "worker_id": "w-1",
                    "allow_no_commit": True,
                }
            )
            factory.return_value = graph

            ci_gate.return_value = (True, [], False, None)

            from src.consumers.engineering import process_engineering_job

            result = await process_engineering_job(
                _make_job(planning_task_id="task-ci"), mock_redis
            )

            # Should have run CI gate (not skipped)
            ci_gate.assert_awaited_once()
            # Normal success path returns "success" (existing convention)
            assert result["status"] in ("success", RunStatus.COMPLETED)
