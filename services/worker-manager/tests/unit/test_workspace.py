from unittest.mock import patch

from src.workspace import (
    create_workspace,
    get_or_create_project_workspace,
    get_workspace_host_path,
    remove_workspace,
)


class TestWorkspace:
    def test_create_workspace_creates_directory(self, tmp_path):
        """create_workspace should create the workspace directory and return its Path."""
        result = create_workspace(str(tmp_path), "worker-123")

        assert result == tmp_path / "worker-123" / "workspace"
        assert result.is_dir()

    def test_create_workspace_is_idempotent(self, tmp_path):
        """Calling create_workspace twice should not raise."""
        create_workspace(str(tmp_path), "worker-123")
        result = create_workspace(str(tmp_path), "worker-123")
        assert result.is_dir()

    def test_get_workspace_host_path_returns_correct_path(self, tmp_path):
        """get_workspace_host_path should return the expected string path."""
        result = get_workspace_host_path(str(tmp_path), "worker-abc")
        expected = str(tmp_path / "worker-abc" / "workspace")
        assert result == expected

    def test_remove_workspace_removes_directory(self, tmp_path):
        """remove_workspace should remove the entire worker directory."""
        create_workspace(str(tmp_path), "worker-456")
        worker_dir = tmp_path / "worker-456"
        assert worker_dir.exists()

        remove_workspace(str(tmp_path), "worker-456")
        assert not worker_dir.exists()

    def test_remove_workspace_ignores_missing(self, tmp_path):
        """remove_workspace should not raise if the directory doesn't exist."""
        # Should not raise
        remove_workspace(str(tmp_path), "nonexistent-worker")


class TestGetOrCreateProjectWorkspace:
    def test_creates_new_workspace(self, tmp_path):
        """New project_id should create directory and return (path, False)."""
        with patch("src.workspace._chown_recursive"):
            ws_path, already_existed = get_or_create_project_workspace(str(tmp_path), "proj-1")

        assert ws_path == tmp_path / "proj-1" / "workspace"
        assert ws_path.is_dir()
        assert already_existed is False

    def test_reuses_existing_workspace(self, tmp_path):
        """Existing project workspace should return (path, True) and touch mtime."""
        # Pre-create the workspace directory
        existing = tmp_path / "proj-1" / "workspace"
        existing.mkdir(parents=True)

        with patch("src.workspace._chown_recursive"):
            ws_path, already_existed = get_or_create_project_workspace(str(tmp_path), "proj-1")

        assert ws_path == existing
        assert already_existed is True


class TestChownRecursive:
    def test_chown_recursive_called_in_create_workspace(self, tmp_path):
        """create_workspace should use _chown_recursive internally."""
        with patch("src.workspace._chown_recursive") as mock_chown:
            create_workspace(str(tmp_path), "worker-1")
            mock_chown.assert_called_once_with(tmp_path / "worker-1")

    def test_chown_recursive_called_in_get_or_create_project_workspace(self, tmp_path):
        """get_or_create_project_workspace should use _chown_recursive internally."""
        with patch("src.workspace._chown_recursive") as mock_chown:
            get_or_create_project_workspace(str(tmp_path), "proj-1")
            mock_chown.assert_called_once_with(tmp_path / "proj-1")
