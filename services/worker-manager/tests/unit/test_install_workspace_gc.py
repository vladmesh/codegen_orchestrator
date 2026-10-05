"""Install writers and workspace cleanup hold the same stable repository inode."""

import fcntl
import os
import time
from unittest.mock import AsyncMock

from fakeredis import aioredis
import pytest

from src.garbage_collector import garbage_collect_workspaces


@pytest.mark.asyncio
async def test_gc_keeps_busy_install_without_worker_and_never_unlinks_lock(tmp_path, monkeypatch):
    root = tmp_path / "repo-install"
    root.mkdir()
    locks = tmp_path / ".catalog-install-locks"
    locks.mkdir()
    lock_path = locks / root.name
    lock = lock_path.open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    inode = lock_path.stat().st_ino
    old = time.time() - 36 * 3600
    os.utime(root, (old, old))
    os.utime(locks, (old, old))
    monkeypatch.setattr("src.garbage_collector.settings.SCAFFOLDED_WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setattr("src.garbage_collector.has_preserved_work", lambda path: False)
    notified = AsyncMock()
    monkeypatch.setattr("src.garbage_collector._notify_workspace_deleted", notified)
    redis = aioredis.FakeRedis(decode_responses=True)
    try:
        await garbage_collect_workspaces(redis)
        assert root.exists()
        assert lock_path.stat().st_ino == inode
        notified.assert_not_awaited()
        fcntl.flock(lock, fcntl.LOCK_UN)
        await garbage_collect_workspaces(redis)
        assert not root.exists()
        assert lock_path.stat().st_ino == inode
        notified.assert_awaited_once_with(root.name)
    finally:
        lock.close()
        await redis.aclose()


@pytest.mark.asyncio
async def test_gc_holds_install_lock_through_cleanup_notification(tmp_path, monkeypatch):
    root = tmp_path / "repo-idle"
    root.mkdir()
    old = time.time() - 36 * 3600
    os.utime(root, (old, old))
    monkeypatch.setattr("src.garbage_collector.settings.SCAFFOLDED_WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setattr("src.garbage_collector.has_preserved_work", lambda path: False)

    async def notified(repo_id):
        assert not root.exists()
        with (tmp_path / ".catalog-install-locks" / repo_id).open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setattr("src.garbage_collector._notify_workspace_deleted", notified)
    redis = aioredis.FakeRedis(decode_responses=True)
    try:
        await garbage_collect_workspaces(redis)
        assert not root.exists()
    finally:
        await redis.aclose()
