import os
import shutil
from pathlib import Path

# UID/GID of the worker user inside containers (always 1000)
WORKER_UID = 1000
WORKER_GID = 1000


def create_workspace(base_path: str, worker_id: str) -> Path:
    """Create workspace directory for a worker.

    Creates /tmp/codegen/workspaces/<worker_id>/workspace/ and returns the Path.
    Ownership is set to worker user (1000:1000) so the container process can write.
    """
    workspace_path = Path(base_path) / worker_id / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    # chown the entire worker directory tree so the worker user can write
    worker_dir = Path(base_path) / worker_id
    for dirpath, dirnames, filenames in os.walk(worker_dir):
        os.chown(dirpath, WORKER_UID, WORKER_GID)
        for filename in filenames:
            os.chown(os.path.join(dirpath, filename), WORKER_UID, WORKER_GID)
    return workspace_path


def get_workspace_host_path(base_path: str, worker_id: str) -> str:
    """Return the host path to the worker's workspace directory."""
    return str(Path(base_path) / worker_id / "workspace")


def remove_workspace(base_path: str, worker_id: str) -> None:
    """Remove the workspace directory for a worker (ignores errors)."""
    worker_dir = Path(base_path) / worker_id
    shutil.rmtree(worker_dir, ignore_errors=True)
