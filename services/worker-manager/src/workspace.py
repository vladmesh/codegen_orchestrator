import shutil
import subprocess
from pathlib import Path


WORKER_OWNER = "1000:1000"


def _resolve_direct_workspace_child(base_path: str, entry_id: str) -> Path:
    """Resolve one workspace entry and refuse paths outside its configured root."""
    root = Path(base_path).resolve()
    entry = Path(entry_id)
    candidate = (root / entry).resolve()

    if entry.is_absolute() or len(entry.parts) != 1 or candidate.parent != root:
        raise ValueError(
            f"Invalid scaffolded workspace identifier {entry_id!r}: it must name one direct child of {root}"
        )

    return candidate


def get_scaffolded_workspace(base_path: str, repo_id: str) -> tuple[Path, bool]:
    """Get path to a pre-scaffolded workspace created by the scaffolder service.

    Scaffolder stores workspaces at base_path/repo_id/ (no nested /workspace/ subdir).

    Returns (workspace_path, exists).
    """
    workspace_path = _resolve_direct_workspace_child(base_path, repo_id)
    return workspace_path, workspace_path.exists()


def remove_workspace(base_path: str, entry_id: str) -> None:
    """Remove a workspace directory (ignores errors)."""
    workspace_dir = _resolve_direct_workspace_child(base_path, entry_id)
    shutil.rmtree(workspace_dir, ignore_errors=True)


def prepare_worker_paths(workspace_path: str | Path, transcript_path: str | Path) -> None:
    """Make host-backed paths writable before launching a hardened worker."""
    workspace = Path(workspace_path)
    transcript = Path(transcript_path)
    if not workspace.is_dir():
        raise RuntimeError(f"Worker workspace is not a directory: {workspace}")

    transcript.mkdir(parents=True, exist_ok=True)
    for path in (workspace, transcript):
        try:
            result = subprocess.run(
                ["chown", "-R", WORKER_OWNER, str(path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not prepare worker-owned path {path}: {exc}") from exc

        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not prepare worker-owned path {path}: {output}")
