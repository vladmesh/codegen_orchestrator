import asyncio
import os

import structlog

logger = structlog.get_logger()

# Configuration
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # 1 minute


async def health_check_worker():
    """Health checker worker - monitors server health via SSH."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
