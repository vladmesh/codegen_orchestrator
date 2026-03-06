"""Unit tests for DeveloperNode commit_sha validation and ScaffoldConfig construction.

Verifies that the developer node rejects success results without a commit_sha,
and correctly builds ScaffoldConfig for new projects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.worker_spawner import SpawnResult


def _make_state(*, action="create", status="scaffolded", modules=None):
    return {
        "project_spec": {
            "id": "proj-1",
            "name": "test-project",
            "status": status,
            "config": {
                "modules": modules or ["backend"],
                "description": "A test project",
            },
            "repository_url": "https://github.com/org/test-project",
        },
        "action": action,
        "errors": [],
    }


class TestDeveloperNodeCommitValidation:
    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_without_commit_sha_returns_blocked(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True but commit_sha=None must return blocked, not done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha=None,
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "blocked"
        assert any("no commit" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_with_commit_sha_returns_done(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True with commit_sha must return done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_failure_still_returns_blocked(self, mock_github_cls, mock_api, mock_spawn):
        """Worker success=False must return blocked (existing behavior, sanity check)."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=False,
            exit_code=1,
            output="",
            error_message="timeout",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "blocked"


class TestScaffoldConfigConstruction:
    """Tests for _build_scaffold_config and scaffold flow."""

    def test_scaffolding_status_creates_config(self):
        """action=create + status=scaffolding → ScaffoldConfig is built."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "My Project",
            "status": "scaffolding",
            "config": {
                "modules": ["backend", "tg_bot"],
                "description": "Build something cool",
            },
        }
        config = node._build_scaffold_config(project_spec, "create")

        assert config is not None
        assert config.project_name == "my-project"
        assert config.modules == "backend,tg_bot"
        assert config.task_description == "Build something cool"
        assert "service-template" in config.template_repo

    def test_feature_action_no_config(self):
        """action=feature → no ScaffoldConfig."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {"name": "test", "status": "scaffolding", "config": {}}
        config = node._build_scaffold_config(project_spec, "feature")
        assert config is None

    def test_scaffolded_status_no_config(self):
        """status=scaffolded → no ScaffoldConfig (already done)."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {"name": "test", "status": "scaffolded", "config": {}}
        config = node._build_scaffold_config(project_spec, "create")
        assert config is None

    def test_draft_status_no_config(self):
        """status=draft → no ScaffoldConfig (engineering worker sets scaffolding first)."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {"name": "test", "status": "draft", "config": {}}
        config = node._build_scaffold_config(project_spec, "create")
        assert config is None

    @pytest.mark.asyncio
    async def test_create_with_draft_status_hard_fails(self):
        """action=create + status=draft → hard fail (stale project dict bug)."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="draft")
        result = await node.run(state)

        assert result["engineering_status"] == "blocked"
        assert any("draft" in e for e in result["errors"])

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_create_with_scaffolded_status_proceeds(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """action=create + status=scaffolded → proceeds normally (scaffold already done)."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="scaffolded")
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        mock_spawn.assert_awaited_once()

    def test_sanitizes_project_name(self):
        """Project name is sanitized for copier."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "My Cool Project!!!",
            "status": "scaffolding",
            "config": {"modules": ["backend"]},
        }
        config = node._build_scaffold_config(project_spec, "create")
        assert config is not None
        assert config.project_name == "my-cool-project"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_scaffold_config_passed_to_spawn(self, mock_github_cls, mock_api, mock_spawn):
        """ScaffoldConfig is forwarded to request_spawn."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="scaffolding")
        await node.run(state)

        mock_spawn.assert_awaited_once()
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["scaffold_config"] is not None
        assert call_kwargs["scaffold_config"].modules == "backend"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_scaffold_failure_sets_status(self, mock_github_cls, mock_api, mock_spawn):
        """Scaffold phase failure → status='scaffold_failed'."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=False,
            exit_code=1,
            output="copier failed",
            error_message="Scaffold phase failed",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="scaffolding")
        result = await node.run(state)

        assert result["engineering_status"] == "blocked"
        # Project should be patched to scaffold_failed
        patch_calls = [str(c) for c in mock_api.patch.call_args_list]
        assert any("scaffold_failed" in c for c in patch_calls)


class TestFeatureFlowIntegration:
    """Tests for action=feature/fix through the full DeveloperNode.run() path."""

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_action_skips_scaffold_and_succeeds(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """action=feature on active project → no scaffold, feature task, done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "test-project",
                "status": "active",
                "config": {"modules": ["backend"], "description": "A todo API"},
                "repository_url": "https://github.com/org/test-project",
            }
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Feature added",
            commit_sha="feat123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="active")
        state["description"] = "Add GET /todos/stats endpoint"
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] == "feat123"

        # Verify scaffold_config is None (no scaffold for features)
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["scaffold_config"] is None

        # Verify task_content uses feature template (not create template)
        assert "existing, working project" in call_kwargs["task_content"]
        assert "Add GET /todos/stats endpoint" in call_kwargs["task_content"]

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_fix_action_uses_fix_template(self, mock_github_cls, mock_api, mock_spawn):
        """action=fix → task title says 'Fix Issue', template says 'existing project'."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Fixed",
            commit_sha="fix456",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="fix", status="active")
        state["description"] = "Fix empty input crash"
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["scaffold_config"] is None
        assert "Fix issue" in call_kwargs["task_title"]
        assert "Fix empty input crash" in call_kwargs["task_content"]

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_on_scaffolded_project_works(self, mock_github_cls, mock_api, mock_spawn):
        """action=feature on scaffolded (not yet deployed) project works."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc789",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="scaffolded")
        state["description"] = "Add logging"
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["scaffold_config"] is None

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_refreshes_project_spec(self, mock_github_cls, mock_api, mock_spawn):
        """action=feature refreshes project from API (picks up latest repo URL etc)."""
        fresh_project = {
            "id": "proj-1",
            "name": "test-project",
            "status": "active",
            "config": {"modules": ["backend"], "description": "A test project"},
            "repository_url": "https://github.com/org/updated-repo-name",
        }
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=fresh_project)
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="active")
        state["description"] = "Some feature"
        await node.run(state)

        # Should have refreshed project spec
        mock_api.get_project.assert_awaited_once_with("proj-1")
        # Repo should use the refreshed URL
        call_kwargs = mock_spawn.call_args[1]
        assert "updated-repo-name" in call_kwargs["repo"]


class TestTaskMessageDescription:
    """Tests that _build_create_task reads description from config, not top-level."""

    def test_description_from_config(self):
        """Description in TASK.md must come from config.description."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend"],
                "description": "Build a REST API for todos",
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="Build a REST API for todos",
            modules=["backend"],
            project_spec=project_spec,
        )
        assert "Build a REST API for todos" in task_md

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_config_description_flows_to_task_message(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """config.description must appear in the task_content passed to request_spawn."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="scaffolded")
        # Override with specific description
        state["project_spec"]["config"]["description"] = "My specific task with audit"
        await node.run(state)

        mock_spawn.assert_awaited_once()
        task_content = mock_spawn.call_args[1]["task_content"]
        assert "My specific task with audit" in task_content

    def test_create_task_includes_env_hints(self):
        """_build_create_task should include env_hints section when hints exist."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend", "tg_bot"],
                "description": "A telegram bot",
                "env_hints": {
                    "ADMIN_TELEGRAM_ID": "Telegram ID of the bot admin",
                    "OPENAI_API_KEY": "OpenAI key for generating responses",
                },
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="A telegram bot",
            modules=["backend", "tg_bot"],
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" in task_md
        assert "ADMIN_TELEGRAM_ID" in task_md
        assert "Telegram ID of the bot admin" in task_md
        assert "OPENAI_API_KEY" in task_md
        assert "os.getenv" in task_md

    def test_create_task_no_env_hints(self):
        """_build_create_task should NOT include env_hints section when empty."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend"],
                "description": "A simple API",
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="A simple API",
            modules=["backend"],
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" not in task_md

    def test_feature_task_includes_env_hints(self):
        """_build_feature_task should include env_hints section when hints exist."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "config": {
                "env_hints": {"API_KEY": "Third-party API key"},
            },
        }
        task_md = node._build_feature_task(
            project_name="test-project",
            description="An existing project",
            modules=["backend"],
            action="feature",
            feature_description="Add search feature",
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" in task_md
        assert "API_KEY" in task_md
        assert "Third-party API key" in task_md

    def test_feature_task_falls_back_to_config_description(self):
        """_build_feature_task falls back to description when feature_description is None."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_feature_task(
            project_name="test-project",
            description="Config description here",
            modules=["backend"],
            action="feature",
            feature_description=None,
            project_spec={},
        )
        assert "Config description here" in task_md
