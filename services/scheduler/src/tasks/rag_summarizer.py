import asyncio

import structlog

from .. import startup

logger = structlog.get_logger()


def _poll_interval() -> int:
    return startup.get_config().get_int("scheduler.rag_summarizer_poll_interval")


async def rag_summarizer_worker() -> None:
    """Background worker to summarize raw chat messages.

    Disabled pending refactoring.
    """
    logger.info("rag_summarizer_worker_disabled_pending_refactor")
    while True:
        await asyncio.sleep(_poll_interval())
