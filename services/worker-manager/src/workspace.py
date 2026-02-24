import os
import shutil
from pathlib import Path

# UID/GID of the worker user inside containers (always 1000)
WORKER_UID = 1000
WORKER_GID = 1000


def _chown_recursive(path: Path) -> None:
    """Set ownership of path and all contents to the worker user (1000:1000)."""
    for dirpath, dirnames, filenames in os.walk(path):
        os.chown(dirpath, WORKER_UID, WORKER_GID)
        for filename in filenames:
            os.chown(os.path.join(dirpath, filename), WORKER_UID, WORKER_GID)


def create_workspace(base_path: str, worker_id: str) -> Path:
    """Create workspace directory for a worker.

    Creates /tmp/codegen/workspaces/<worker_id>/workspace/ and returns the Path.
    Ownership is set to worker user (1000:1000) so the container process can write.
    """
    workspace_path = Path(base_path) / worker_id / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    _chown_recursive(Path(base_path) / worker_id)
    return workspace_path


def get_or_create_project_workspace(base_path: str, project_id: str) -> tuple[Path, bool]:
    """Get or create workspace for a project.

    Returns (workspace_path, already_existed).
    """
    workspace_path = Path(base_path) / project_id / "workspace"
    already_existed = workspace_path.exists()
    workspace_path.mkdir(parents=True, exist_ok=True)
    if already_existed:
        os.utime(workspace_path)  # Touch mtime for GC age calculation
    _chown_recursive(Path(base_path) / project_id)
    return workspace_path, already_existed


def get_workspace_host_path(base_path: str, worker_id: str) -> str:
    """Return the host path to the worker's workspace directory."""
    return str(Path(base_path) / worker_id / "workspace")


def remove_workspace(base_path: str, worker_id: str) -> None:
    """Remove the workspace directory for a worker (ignores errors)."""
    worker_dir = Path(base_path) / worker_id
    shutil.rmtree(worker_dir, ignore_errors=True)
