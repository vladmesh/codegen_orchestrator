"""Unit tests for scaffold_project function."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestScaffoldProject:
    """Tests for scaffold_project with mocked git, copier, and GitHub."""

    @pytest.fixture
    def mock_github_token(self):
        """Mock GitHub token retrieval."""
        with patch("main.get_github_token", new_callable=AsyncMock) as mock:
            mock.return_value = "ghp_test_token_123"
            yield mock

    @pytest.fixture
    def mock_git(self):
        """Mock _run_git to simulate successful git operations."""
        with patch("main._run_git") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            yield mock

    @pytest.fixture
    def mock_copier(self):
        """Mock subprocess.run for copier execution."""
        with patch("subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            yield mock

    @pytest.fixture
    def mock_shutil_which(self):
        """Mock shutil.which to return valid paths."""
        with patch("shutil.which") as mock:
            mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
            yield mock

    @pytest.mark.asyncio
    async def test_clones_repo_with_auth_token(
        self, mock_github_token, mock_git, mock_copier, mock_shutil_which
    ):
        """Should clone repo using GitHub token in URL."""
        from main import scaffold_project

        with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/scaffold_test")  # noqa: S108
            mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

            await scaffold_project(
                repo_full_name="vladmesh/test-repo",
                project_name="test-project",
                project_id="proj-123",
                modules="backend",
            )

            # Verify git clone was called
            clone_calls = [c for c in mock_git.call_args_list if "clone" in c[0]]
            assert len(clone_calls) >= 1

            # Token should be in clone URL
            clone_url = clone_calls[0][0][1]
            assert "ghp_test_token_123" in clone_url
            assert "vladmesh/test-repo" in clone_url

    @pytest.mark.asyncio
    async def test_runs_copier_with_correct_args(
        self, mock_github_token, mock_git, mock_copier, mock_shutil_which
    ):
        """Should run copier with project_name and modules data args."""
        from main import scaffold_project

        with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/scaffold_test")  # noqa: S108
            mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

            await scaffold_project(
                repo_full_name="vladmesh/test-repo",
                project_name="My Test Project",
                project_id="proj-123",
                modules="backend,frontend",
            )

            # Verify copier was called with correct data args
            copier_call = mock_copier.call_args
            cmd_list = copier_call[0][0]

            # Find --data arguments
            data_args = []
            for i, arg in enumerate(cmd_list):
                if arg == "--data" and i + 1 < len(cmd_list):
                    data_args.append(cmd_list[i + 1])

            assert any("project_name=" in arg for arg in data_args)
            assert any("modules=backend,frontend" in arg for arg in data_args)

    @pytest.mark.asyncio
    async def test_commits_and_pushes_changes(
        self, mock_github_token, mock_git, mock_copier, mock_shutil_which
    ):
        """Should commit changes and push to origin main."""
        from main import scaffold_project

        with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/scaffold_test")  # noqa: S108
            mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

            result = await scaffold_project(
                repo_full_name="vladmesh/test-repo",
                project_name="test-project",
                project_id="proj-123",
                modules="backend",
            )

            assert result is True

            # Verify git operations: add, commit, push
            git_commands = [c[0] for c in mock_git.call_args_list]
            assert any("add" in cmd for cmd in git_commands)
            assert any("commit" in cmd for cmd in git_commands)
            assert any("push" in cmd for cmd in git_commands)

    @pytest.mark.asyncio
    async def test_returns_false_on_clone_failure(self, mock_github_token, mock_shutil_which):
        """Should return False if git clone fails."""
        from main import scaffold_project

        with patch("main._run_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=1, stderr="clone failed")

            with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/scaffold_test")  # noqa: S108
                mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

                result = await scaffold_project(
                    repo_full_name="vladmesh/test-repo",
                    project_name="test-project",
                    project_id="proj-123",
                    modules="backend",
                )

                assert result is False

    @pytest.mark.asyncio
    async def test_sanitizes_project_name(
        self, mock_github_token, mock_git, mock_copier, mock_shutil_which
    ):
        """Project name should be sanitized: lowercase, hyphens, no special chars."""
        from main import scaffold_project

        with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
            mock_tmpdir.return_value.__enter__ = MagicMock(return_value="/tmp/scaffold_test")  # noqa: S108
            mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

            await scaffold_project(
                repo_full_name="vladmesh/test-repo",
                project_name="My   Test_Project!!!",
                project_id="proj-123",
                modules="backend",
            )

            # Check copier received sanitized name
            copier_call = mock_copier.call_args
            cmd_list = copier_call[0][0]

            # Find project_name data arg
            for i, arg in enumerate(cmd_list):
                if arg == "--data" and i + 1 < len(cmd_list):
                    if "project_name=" in cmd_list[i + 1]:
                        name_arg = cmd_list[i + 1]
                        # Should be lowercase with hyphens
                        assert "my-test-project" in name_arg.lower()
                        assert "_" not in name_arg.split("=")[1]
                        assert "!" not in name_arg
