from src.workspace import create_workspace, get_workspace_host_path, remove_workspace


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
