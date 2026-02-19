import shutil
from pathlib import Path


def create_workspace(base_path: str, worker_id: str) -> Path:
    """Create workspace directory for a worker.

    Creates /tmp/codegen/workspaces/<worker_id>/workspace/ and returns the Path.
    """
    workspace_path = Path(base_path) / worker_id / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    return workspace_path


def get_workspace_host_path(base_path: str, worker_id: str) -> str:
    """Return the host path to the worker's workspace directory."""
    return str(Path(base_path) / worker_id / "workspace")


def remove_workspace(base_path: str, worker_id: str) -> None:
    """Remove the workspace directory for a worker (ignores errors)."""
    worker_dir = Path(base_path) / worker_id
    shutil.rmtree(worker_dir, ignore_errors=True)
