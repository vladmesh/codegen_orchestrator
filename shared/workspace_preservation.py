"""A collector cannot delete a checkout whose unpublished work is unproved."""

import fcntl
import os
from pathlib import Path
import subprocess

from shared.git_snapshot import SnapshotRefusal, object_snapshot

CATALOG_INSTALL_LOCKS = ".catalog-install-locks"
#: `<repository id>/<operation id>`: one private checkout per catalog install attempt,
#: registered as a worktree of its repository's workspace. The collector never sweeps it.
CATALOG_INSTALL_ATTEMPTS = ".catalog-install-attempts"


def acquire_install_workspace_lock(workspace: Path):
    """Fence install and GC on a stable inode; callers close after owned work ends.

    Neither cleanup nor execution unlinks the lock or its metadata directory.
    """
    directory = workspace.parent / CATALOG_INSTALL_LOCKS
    directory.mkdir(exist_ok=True)
    lock = (directory / workspace.name).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


def has_preserved_work(workspace: Path) -> bool:
    try:
        if workspace.lstat().st_uid != os.getuid():
            return True
        # A retained install attempt is a worktree of this repository; its evidence needs it.
        worktrees = workspace / ".git" / "worktrees"
        if worktrees.is_dir() and any(worktrees.iterdir()):
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
