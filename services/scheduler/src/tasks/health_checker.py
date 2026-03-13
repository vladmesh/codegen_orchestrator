import asyncio
import os

import structlog

logger = structlog.get_logger()

# Configuration
_interval = os.getenv("HEALTH_CHECK_INTERVAL")
if not _interval:
    raise RuntimeError("HEALTH_CHECK_INTERVAL is not set")
HEALTH_CHECK_INTERVAL = int(_interval)


async def health_check_worker():
    """Health checker worker - monitors server health via SSH."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
