"""Run one canonical server health probe for the ephemeral Stand contour."""

import asyncio
import os

from . import startup
from .clients.api import api_client
from .tasks.health_checker import _check_server


async def probe(target_handle: str) -> None:
    """Initialize scheduler runtime state and health-check one target."""
    startup.init_config()
    server = await api_client.get_server(target_handle)
    await _check_server(server)


def main() -> None:
    """Probe the target selected by the Stand workflow."""
    target_handle = os.getenv("TARGET_HANDLE")
    if not target_handle:
        raise RuntimeError("TARGET_HANDLE is not set")
    asyncio.run(probe(target_handle))


if __name__ == "__main__":
    main()
