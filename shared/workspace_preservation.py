"""A collector cannot delete a checkout whose unpublished work is unproved."""

from pathlib import Path
import subprocess


def has_preserved_work(workspace: Path) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/git", "rev-list", "HEAD", "--branches", "--not", "--remotes"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode != 0 or bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True
