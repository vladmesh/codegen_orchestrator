"""LangGraph worker main orchestration."""

import asyncio

from shared.logging_config import setup_logging

from .events import listen_worker_events
from .message_processor import consume_chat_stream
from .provisioner import listen_provisioner_triggers
from .utils import periodic_memory_stats


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    await asyncio.gather(
        consume_chat_stream(),
        listen_provisioner_triggers(),
        listen_worker_events(),
        periodic_memory_stats(),
    )


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
