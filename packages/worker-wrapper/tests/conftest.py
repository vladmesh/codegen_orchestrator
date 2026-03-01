"""Shared test fixtures for worker-wrapper tests."""


class MockProcess:
    """Mock for asyncio.create_subprocess_exec return value."""

    def __init__(self, stdout, stderr, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr
