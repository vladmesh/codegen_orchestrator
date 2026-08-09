import subprocess
from unittest.mock import patch

import pytest

from src.workspace import get_scaffolded_workspace, prepare_worker_paths, remove_workspace


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

    @pytest.mark.parametrize("repo_id", ["/tmp/outside", "../outside", "repo-123/nested", ".", ".."])
    def test_rejects_repo_ids_that_are_not_direct_workspace_children(self, tmp_path, repo_id):
        with pytest.raises(ValueError, match="direct child"):
            get_scaffolded_workspace(str(tmp_path), repo_id)

    def test_rejects_child_symlink_that_resolves_outside_workspace_root(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}"
        outside.mkdir()
        (tmp_path / "repo-escape").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="direct child"):
            get_scaffolded_workspace(str(tmp_path), "repo-escape")


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

    def test_refuses_unsafe_entry_without_removing_outside_path(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.touch()

        with pytest.raises(ValueError, match="direct child"):
            remove_workspace(str(tmp_path), f"../{outside.name}")

        assert marker.exists()


class TestPrepareWorkerPaths:
    def test_chowns_workspace_and_transcript_paths_before_launch(self, tmp_path):
        workspace = tmp_path / "workspace"
        transcript = tmp_path / "transcripts"
        workspace.mkdir()

        with patch("src.workspace.subprocess.run") as run:
            run.return_value.returncode = 0

            prepare_worker_paths(workspace, transcript)

        assert transcript.is_dir()
        assert run.call_args_list == [
            ((["chown", "-R", "1000:1000", str(workspace)],), {"capture_output": True, "text": True}),
            ((["chown", "-R", "1000:1000", str(transcript)],), {"capture_output": True, "text": True}),
        ]

    def test_chown_failure_is_reported(self, tmp_path):
        workspace = tmp_path / "workspace"
        transcript = tmp_path / "transcripts"
        workspace.mkdir()

        with patch("src.workspace.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["chown"], returncode=1, stderr="Operation not permitted"
            )

            with pytest.raises(RuntimeError, match="Operation not permitted"):
                prepare_worker_paths(workspace, transcript)
