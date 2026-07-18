"""Unit tests for engineering worker fail-fast checks.

Tests commit_sha gate in _handle_engineering_success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_repository

from src.consumers.engineering import EngineeringSuccessParams


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.publish_message = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    """Patch api_client in both engineering and engineering_result_handler modules."""
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_project = AsyncMock(return_value=None)
        api.get_primary_repository = AsyncMock(
            return_value=make_repository(git_url="https://github.com/org/test-project")
        )
        with patch("src.consumers.engineering_result_handler.api_client", api):
            yield api


def _project():
    return make_project(name="test-project", config={"modules": ["backend"]})


class TestHandleEngineeringSuccess:
    @pytest.mark.asyncio
    async def test_no_commit_sha_fails_fast(self, mock_redis, mock_api):
        """commit_sha=None must return failed, not proceed to CI/deploy."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": None,
        }

        out = await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
            )
        )

        assert out["status"] == "failed"
        error_lower = out.get("error", "").lower()
        assert "commit_sha" in error_lower or "commit" in error_lower

        # Task must be patched as failed
        mock_api.patch.assert_called()
        patch_calls = [c for c in mock_api.patch.call_args_list if "runs/" in str(c)]
        assert any("failed" in str(c) for c in patch_calls)

        # Callback must be "failed" (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        failed_events = [c for c in flat_calls if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1

        # Deploy queue must NOT have been written to
        pm_calls = mock_redis.publish_message.call_args_list
        deploy_calls = [c for c in pm_calls if "deploy" in str(c[0][0])]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    async def test_with_commit_sha_proceeds(self, mock_redis, mock_api):
        """commit_sha present must proceed to deploy."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        out = await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
            )
        )

        assert out["status"] == "success"
        assert out["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_deploy_message_includes_user_id(self, mock_redis, mock_api):
        """DeployMessage queued must include user_id (BUG 17)."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {"engineering_status": "done", "commit_sha": "abc123"}

        await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="625038902",
            )
        )

        # Find the deploy queue publish_message call
        from shared.queues import DEPLOY_QUEUE

        pm_calls = mock_redis.publish_message.call_args_list
        deploy_calls = [c for c in pm_calls if c[0][0] == DEPLOY_QUEUE]
        assert len(deploy_calls) == 1, (
            f"Expected 1 deploy queue call, got {len(deploy_calls)}. "
            f"All publish_message streams: {[c[0][0] for c in pm_calls]}"
        )

        deploy_msg = deploy_calls[0][0][1]
        assert deploy_msg.user_id == "625038902", f"user_id mismatch. Full deploy_msg: {deploy_msg}"

    @pytest.mark.asyncio
    async def test_deploy_message_includes_action(self, mock_redis, mock_api):
        """DeployMessage queued must include action from engineering job (#21)."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {"engineering_status": "done", "commit_sha": "abc123"}

        await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
                action="feature",
            )
        )

        from shared.queues import DEPLOY_QUEUE

        pm_calls = mock_redis.publish_message.call_args_list
        deploy_calls = [c for c in pm_calls if c[0][0] == DEPLOY_QUEUE]
        assert len(deploy_calls) == 1

        deploy_msg = deploy_calls[0][0][1]
        assert deploy_msg.action == "feature"


class TestNotificationDecoupling:
    """Tests that notification type is decoupled from deploy trigger."""

    @pytest.mark.asyncio
    async def test_ci_passed_sends_progress_when_deploying(self, mock_redis, mock_api):
        """skip_deploy=False → event type is 'progress', not 'completed'."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
            )
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "progress" event with deploy message
        progress_events = [c for c in callback_events if c[0][1].get("event") == "progress"]
        assert any("deploying" in c[0][1].get("text", "").lower() for c in progress_events)

        # There should NOT be a "completed" event from engineering worker
        completed_events = [c for c in callback_events if c[0][1].get("event") == "completed"]
        assert len(completed_events) == 0

    @pytest.mark.asyncio
    async def test_ci_passed_sends_completed_when_skip_deploy(self, mock_redis, mock_api):
        """skip_deploy=True → event type is 'completed' (this IS the final step)."""
        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=True,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
            )
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "completed" event
        completed_events = [c for c in callback_events if c[0][1].get("event") == "completed"]
        assert len(completed_events) == 1

    @pytest.mark.asyncio
    async def test_deploy_trigger_failure_publishes_failed_event(self, mock_redis, mock_api):
        """When deploy queuing fails, user gets a 'failed' notification."""
        # Make deploy task creation fail
        mock_api.post.side_effect = RuntimeError("API unreachable")

        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            EngineeringSuccessParams(
                result=result_data,
                task_id="eng-1",
                project=_project(),
                callback_stream="po:response:abc",
                redis=mock_redis,
                skip_deploy=False,
                developer_started_at=datetime.now(UTC),
                user_id="u1",
            )
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "failed" event about deploy trigger
        failed_events = [c for c in callback_events if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1


class TestFeatureActionFlow:
    """Tests for action=feature through process_engineering_job."""

    @pytest.mark.asyncio
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch(
        "src.consumers.engineering_result_handler.publish_callback_event", new_callable=AsyncMock
    )
    async def test_feature_skips_repo_creation(
        self,
        mock_rh_publish,
        mock_rh_delete,
        mock_publish,
        mock_allocator,
        mock_create_subgraph,
        mock_redis,
        mock_api,
    ):
        """action=feature on active project must NOT create repo or set secrets."""
        from src.consumers.engineering import process_engineering_job

        # Project is active with existing repo
        mock_api.get_project = AsyncMock(
            return_value=make_project(
                name="test-project",
                status="active",
                config={"modules": ["backend"], "description": "A todo API"},
            )
        )
        # Resource allocator returns existing allocations
        mock_allocator.run = AsyncMock(
            return_value={
                "allocated_resources": {
                    "srv1:8001": {
                        "server_handle": "srv1",
                        "port": 8001,
                        "server_ip": "1.2.3.4",
                        "service_name": "backend",
                        "application_id": 42,
                    }
                },
                "errors": [],
            }
        )

        # Subgraph returns success
        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "feat123",
                "worker_id": "w1",
            }
        )
        mock_create_subgraph.return_value = mock_subgraph

        result = await process_engineering_job(
            {
                "task_id": "eng-feat-1",
                "project_id": "proj-1",
                "action": "feature",
                "description": "Add stats endpoint",
                "user_id": "u1",
                "callback_stream": "po:input",
            },
            mock_redis,
        )

        assert result["status"] == "success"

        # Verify action=feature was passed to subgraph
        subgraph_input = mock_subgraph.ainvoke.call_args[0][0]
        assert subgraph_input["action"] == "feature"
        assert subgraph_input["description"] == "Add stats endpoint"

        # Verify no repo creation was attempted (no call to _create_repo_and_set_secrets)
        # The project is active, so the draft checks should not trigger
        create_calls = [c for c in mock_api.patch.call_args_list if "scaffolding" in str(c)]
        assert len(create_calls) == 0

    @pytest.mark.asyncio
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch(
        "src.consumers.engineering_result_handler.publish_callback_event", new_callable=AsyncMock
    )
    async def test_feature_reuses_existing_allocations(
        self,
        mock_rh_publish,
        mock_rh_delete,
        mock_publish,
        mock_allocator,
        mock_create_subgraph,
        mock_redis,
        mock_api,
    ):
        """action=feature must reuse existing server/port allocations."""
        from src.consumers.engineering import process_engineering_job

        mock_api.get_project = AsyncMock(
            return_value=make_project(
                name="test-project",
                status="active",
                config={"modules": ["backend"], "description": "A todo API"},
            )
        )

        # Resource allocator returns existing allocations
        mock_allocator.run = AsyncMock(
            return_value={
                "allocated_resources": {
                    "vps-1:8042": {
                        "server_handle": "vps-1",
                        "port": 8042,
                        "server_ip": "1.2.3.4",
                        "service_name": "backend",
                        "application_id": 42,
                    }
                },
                "errors": [],
            }
        )

        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "feat456",
                "worker_id": "w2",
            }
        )
        mock_create_subgraph.return_value = mock_subgraph

        await process_engineering_job(
            {
                "task_id": "eng-feat-2",
                "project_id": "proj-1",
                "action": "feature",
                "description": "Add feature",
                "user_id": "u1",
                "callback_stream": "po:input",
            },
            mock_redis,
        )

        # Verify allocations were passed to subgraph
        subgraph_input = mock_subgraph.ainvoke.call_args[0][0]
        assert "vps-1:8042" in subgraph_input["allocated_resources"]

    @pytest.mark.asyncio
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch(
        "src.consumers.engineering_result_handler.publish_callback_event", new_callable=AsyncMock
    )
    async def test_feature_on_draft_project_warns_but_continues(
        self,
        mock_rh_publish,
        mock_rh_delete,
        mock_publish,
        mock_allocator,
        mock_create_subgraph,
        mock_redis,
        mock_api,
    ):
        """action=feature on draft project logs warning but proceeds (no scaffold_failed)."""
        from shared.contracts.dto.project import ProjectStatus
        from src.consumers.engineering import process_engineering_job

        mock_api.get_project = AsyncMock(
            return_value=make_project(
                name="test-project",
                status=ProjectStatus.DRAFT.value,
                config={"modules": ["backend"], "description": "A test"},
            )
        )
        mock_allocator.run = AsyncMock(
            return_value={
                "allocated_resources": {
                    "srv1:8001": {
                        "server_handle": "srv1",
                        "port": 8001,
                        "server_ip": "1.2.3.4",
                        "service_name": "backend",
                        "application_id": 42,
                    }
                },
                "errors": [],
            }
        )

        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "abc123",
                "worker_id": "w1",
            }
        )
        mock_create_subgraph.return_value = mock_subgraph

        result = await process_engineering_job(
            {
                "task_id": "eng-feat-3",
                "project_id": "proj-1",
                "action": "feature",
                "description": "Add feature",
                "user_id": "u1",
                "callback_stream": "po:input",
            },
            mock_redis,
        )

        # Draft + feature proceeds (with warning), no longer fails fast
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch(
        "src.consumers.engineering_result_handler.publish_callback_event", new_callable=AsyncMock
    )
    async def test_feature_triggers_auto_deploy(
        self,
        mock_rh_publish,
        mock_rh_delete,
        mock_publish,
        mock_allocator,
        mock_create_subgraph,
        mock_redis,
        mock_api,
    ):
        """action=feature with skip_deploy=False must auto-trigger deploy."""
        from src.consumers.engineering import process_engineering_job

        mock_api.get_project = AsyncMock(
            return_value=make_project(
                name="test-project",
                status="active",
                config={"modules": ["backend"], "description": "A todo API"},
            )
        )
        mock_allocator.run = AsyncMock(
            return_value={
                "allocated_resources": {
                    "srv1:8001": {
                        "server_handle": "srv1",
                        "port": 8001,
                        "server_ip": "1.2.3.4",
                        "service_name": "backend",
                        "application_id": 42,
                    }
                },
                "errors": [],
            }
        )

        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "feat789",
                "worker_id": "w3",
            }
        )
        mock_create_subgraph.return_value = mock_subgraph

        result = await process_engineering_job(
            {
                "task_id": "eng-feat-4",
                "project_id": "proj-1",
                "action": "feature",
                "skip_deploy": False,
                "description": "Add feature",
                "user_id": "u1",
                "callback_stream": "po:input",
            },
            mock_redis,
        )

        assert result["status"] == "success"
        assert result["deploy_task_id"] is not None

        # Verify deploy was queued
        from shared.queues import DEPLOY_QUEUE

        pm_calls = mock_redis.publish_message.call_args_list
        deploy_calls = [c for c in pm_calls if c[0][0] == DEPLOY_QUEUE]
        assert len(deploy_calls) == 1

        deploy_msg = deploy_calls[0][0][1]
        assert deploy_msg.project_id  # project_id is populated from ProjectDTO.id
        assert deploy_msg.user_id == "u1"

    @pytest.mark.asyncio
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch(
        "src.consumers.engineering_result_handler.publish_callback_event", new_callable=AsyncMock
    )
    async def test_feature_description_fallback_to_config(
        self,
        mock_rh_publish,
        mock_rh_delete,
        mock_publish,
        mock_allocator,
        mock_create_subgraph,
        mock_redis,
        mock_api,
    ):
        """When description is None, falls back to project config description."""
        from src.consumers.engineering import process_engineering_job

        mock_api.get_project = AsyncMock(
            return_value=make_project(
                name="test-project",
                status="active",
                config={"modules": ["backend"], "description": "Original description"},
            )
        )
        mock_allocator.run = AsyncMock(
            return_value={
                "allocated_resources": {
                    "srv1:8001": {
                        "server_handle": "srv1",
                        "port": 8001,
                        "server_ip": "1.2.3.4",
                        "service_name": "backend",
                        "application_id": 42,
                    }
                },
                "errors": [],
            }
        )

        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "abc",
                "worker_id": "w4",
            }
        )
        mock_create_subgraph.return_value = mock_subgraph

        await process_engineering_job(
            {
                "task_id": "eng-feat-5",
                "project_id": "proj-1",
                "action": "feature",
                "description": None,
                "user_id": "u1",
                "callback_stream": "po:input",
            },
            mock_redis,
        )

        # Subgraph should receive fallback description
        subgraph_input = mock_subgraph.ainvoke.call_args[0][0]
        assert subgraph_input["description"] == "Original description"


class TestCreateRepoAndSetSecrets:
    """Tests for _create_repo_and_set_secrets (replaced _trigger_scaffolding)."""

    @pytest.fixture
    def mock_api(self):
        """Patch api_client in _repo_setup module (where the function lives)."""
        with patch("src.consumers._repo_setup.api_client") as api:
            api.patch = AsyncMock()
            api.post = AsyncMock()
            yield api

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict(
        "os.environ",
        {
            "GITHUB_ORG": "test-org",
            "ORCHESTRATOR_HOSTNAME": "registry.example.com",
            "REGISTRY_USER": "admin",
            "REGISTRY_PASSWORD": "secret",
        },
    )
    async def test_happy_path(self, mock_gh_cls, mock_api):
        """Creates repo, sets secrets, updates project status."""
        from src.consumers._repo_setup import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock()
        mock_gh.get_org_token = AsyncMock(return_value="ghs_token")
        mock_gh.set_repository_secrets = AsyncMock(return_value=3)

        project = make_project(name="my-project")

        await _create_repo_and_set_secrets(project)

        # Repo was created
        mock_gh.create_repo.assert_awaited_once_with(
            org="test-org",
            name="my-project",
            description="Project: my-project",
            private=True,
        )

        # Secrets were set
        mock_gh.set_repository_secrets.assert_awaited_once()
        secrets_arg = mock_gh.set_repository_secrets.call_args[0][2]
        assert secrets_arg["REGISTRY_URL"] == "registry.example.com"
        assert secrets_arg["REGISTRY_USER"] == "admin"
        assert secrets_arg["REGISTRY_PASSWORD"] == "secret"  # noqa: S105

        # Repository entity created (project stays draft — no status patch)
        mock_api.post.assert_called()
        post_calls = [c for c in mock_api.post.call_args_list if "repositories/" in str(c)]
        assert len(post_calls) == 1

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict(
        "os.environ",
        {
            "GITHUB_ORG": "test-org",
            "ORCHESTRATOR_HOSTNAME": "registry.example.com",
            "REGISTRY_USER": "admin",
            "REGISTRY_PASSWORD": "secret",
        },
    )
    async def test_repo_already_exists_fails_fast(self, mock_gh_cls, mock_api):
        """Fails fast when repo already exists (stale state from previous run)."""
        from src.consumers._repo_setup import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock(side_effect=Exception("422: already exists"))

        project = make_project(name="existing-project")

        with pytest.raises(RuntimeError, match="already exists"):
            await _create_repo_and_set_secrets(project)

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict("os.environ", {"GITHUB_ORG": "test-org"}, clear=False)
    async def test_missing_registry_env_warns(self, mock_gh_cls, mock_api):
        """Missing registry env vars logs warning but doesn't fail."""
        import os

        from src.consumers._repo_setup import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock()

        # Clear registry env vars
        env = os.environ.copy()
        for key in ("ORCHESTRATOR_HOSTNAME", "REGISTRY_USER", "REGISTRY_PASSWORD"):
            env.pop(key, None)

        project = make_project(name="test")

        with patch.dict("os.environ", env, clear=True):
            await _create_repo_and_set_secrets(project)

        # Repo still created, but set_repository_secrets NOT called
        mock_gh.create_repo.assert_awaited_once()
        mock_gh.set_repository_secrets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_github_org_raises(self, mock_api):
        """Raises RuntimeError when GITHUB_ORG is not set."""
        from src.consumers._repo_setup import _create_repo_and_set_secrets

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="GITHUB_ORG"):
                await _create_repo_and_set_secrets(make_project(name="x"))
