"""Shared test fixtures for worker-wrapper tests."""

import pytest


@pytest.fixture(autouse=True)
def _workspace_context_files(monkeypatch, tmp_path):
    """Keep context-file writes isolated from the container-only workspace path."""
    monkeypatch.setattr("worker_wrapper.wrapper.TASK_MD_PATH", str(tmp_path / "TASK.md"))
    monkeypatch.setattr("worker_wrapper.wrapper.STORY_DIR", str(tmp_path / ".story"))


class MockProcess:
    """Mock for asyncio.create_subprocess_exec return value."""

    def __init__(self, stdout, stderr, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr
