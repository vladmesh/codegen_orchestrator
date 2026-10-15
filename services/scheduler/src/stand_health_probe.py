"""Run one canonical server health probe for the ephemeral Stand contour."""

import asyncio
import os

from . import startup
from .clients.api import api_client
from .tasks.health_checker import _check_server


async def probe(target_handle: str) -> None:
    """Initialize scheduler config and health-check one target beside its config refresher."""
    config = await startup.init_config(startup.INFRASTRUCTURE_REQUIRED_KEYS)
    refresher = asyncio.create_task(config.refresh_forever())
    try:
        server = await api_client.get_server(target_handle)
        await _check_server(server)
    finally:
        refresher.cancel()
        await asyncio.gather(refresher, return_exceptions=True)


def main() -> None:
    """Probe the target selected by the Stand workflow."""
    target_handle = os.getenv("TARGET_HANDLE")
    if not target_handle:
        raise RuntimeError("TARGET_HANDLE is not set")
    asyncio.run(probe(target_handle))


if __name__ == "__main__":
    main()
