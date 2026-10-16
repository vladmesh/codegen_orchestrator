"""Shared test fixtures for worker-wrapper tests."""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _workspace_context_files(monkeypatch, tmp_path):
    """Keep context-file writes isolated from the container-only workspace path."""
    monkeypatch.setattr("worker_wrapper.wrapper.TASK_MD_PATH", str(tmp_path / "TASK.md"))
    monkeypatch.setattr("worker_wrapper.wrapper.STORY_DIR", str(tmp_path / ".story"))


class FakePipe:
    """An agent output pipe whose ``data`` is readable at once and whose EOF waits for ``eof``."""

    def __init__(self, data: bytes = b"", eof: asyncio.Event | None = None):
        self._data = data
        self._eof = eof

    async def read(self, n: int = -1) -> bytes:
        if (n < 0 or not self._data) and self._eof is not None:
            await self._eof.wait()
        size = len(self._data) if n < 0 else n
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


class MockProcess:
    """Mock for asyncio.create_subprocess_exec return value."""

    def __init__(self, stdout, stderr, returncode=0):
        self.stdout = FakePipe(stdout)
        self.stderr = FakePipe(stderr)
        self.returncode = returncode

    async def wait(self):
        """Match asyncio.subprocess.Process.wait's awaitable contract."""
        return self.returncode
