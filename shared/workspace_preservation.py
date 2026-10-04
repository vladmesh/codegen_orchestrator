"""A collector cannot delete a checkout whose unpublished work is unproved."""

import os
from pathlib import Path
import subprocess

from shared.git_snapshot import SnapshotRefusal, object_snapshot


def has_preserved_work(workspace: Path) -> bool:
    try:
        if workspace.lstat().st_uid != os.getuid():
            return True
        with object_snapshot(workspace) as (clean, env, source):
            result = subprocess.run(  # noqa: S603
                ["/usr/bin/git", "rev-list", "HEAD", "--branches", "--not", "--remotes"],
                cwd=clean,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            source.check()
            return result.returncode != 0 or bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, SnapshotRefusal):
        return True
