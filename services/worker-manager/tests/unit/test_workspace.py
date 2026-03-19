from src.workspace import (
    get_scaffolded_workspace,
    remove_workspace,
)


class TestGetScaffoldedWorkspace:
    def test_returns_path_and_exists_true(self, tmp_path):
        """Existing scaffolded workspace should return (path, True)."""
        ws_dir = tmp_path / "repo-123"
        ws_dir.mkdir()
        path, exists = get_scaffolded_workspace(str(tmp_path), "repo-123")
        assert path == ws_dir
        assert exists is True

    def test_returns_path_and_exists_false(self, tmp_path):
        """Missing workspace should return (path, False)."""
        path, exists = get_scaffolded_workspace(str(tmp_path), "repo-456")
        assert path == tmp_path / "repo-456"
        assert exists is False

    def test_path_is_base_slash_repo_id(self, tmp_path):
        """Path should be base_path/repo_id (no nested /workspace/ subdir)."""
        path, _ = get_scaffolded_workspace(str(tmp_path), "repo-abc")
        assert str(path) == str(tmp_path / "repo-abc")


class TestRemoveWorkspace:
    def test_removes_directory(self, tmp_path):
        """remove_workspace should remove the entire directory."""
        ws_dir = tmp_path / "repo-456"
        ws_dir.mkdir()
        (ws_dir / "file.txt").touch()
        assert ws_dir.exists()

        remove_workspace(str(tmp_path), "repo-456")
        assert not ws_dir.exists()

    def test_ignores_missing(self, tmp_path):
        """remove_workspace should not raise if directory doesn't exist."""
        remove_workspace(str(tmp_path), "nonexistent")
