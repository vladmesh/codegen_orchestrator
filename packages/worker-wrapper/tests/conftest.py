"""Shared test fixtures for worker-wrapper tests."""

import pytest


@pytest.fixture(autouse=True)
def _workspace_context_files(monkeypatch, tmp_path):
    """Keep every workspace path isolated from the real container mount."""
    workspace = tmp_path / "workspace"
    story_dir = tmp_path / ".story"
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr("worker_wrapper.wrapper.TASK_MD_PATH", str(tmp_path / "TASK.md"))
    monkeypatch.setattr("worker_wrapper.wrapper.STORY_DIR", str(story_dir))
    monkeypatch.setattr("worker_wrapper.wrapper.OLD_TASKS_DIR", str(story_dir / "old_tasks"))


class MockProcess:
    """Mock for asyncio.create_subprocess_exec return value."""

    def __init__(self, stdout, stderr, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr

    async def wait(self):
        """Match asyncio.subprocess.Process.wait's awaitable contract."""
        return self.returncode
