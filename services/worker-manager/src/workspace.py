import shutil
from pathlib import Path


def get_scaffolded_workspace(base_path: str, repo_id: str) -> tuple[Path, bool]:
    """Get path to a pre-scaffolded workspace created by the scaffolder service.

    Scaffolder stores workspaces at base_path/repo_id/ (no nested /workspace/ subdir).

    Returns (workspace_path, exists).
    """
    workspace_path = Path(base_path) / repo_id
    return workspace_path, workspace_path.exists()


def remove_workspace(base_path: str, entry_id: str) -> None:
    """Remove a workspace directory (ignores errors)."""
    workspace_dir = Path(base_path) / entry_id
    shutil.rmtree(workspace_dir, ignore_errors=True)
