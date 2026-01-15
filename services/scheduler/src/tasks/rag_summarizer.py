import asyncio

import structlog

logger = structlog.get_logger()

SUMMARY_POLL_INTERVAL = 30


async def rag_summarizer_worker() -> None:
    """Background worker to summarize raw chat messages.

    Disabled pending refactoring.
    """
    logger.info("rag_summarizer_worker_disabled_pending_refactor")
    while True:
        await asyncio.sleep(SUMMARY_POLL_INTERVAL)
