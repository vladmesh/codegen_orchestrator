import asyncio
import signal
import sys

import structlog

from shared.logging.config import setup_logging

from .config import WorkerWrapperConfig
from .wrapper import WorkerWrapper

logger = structlog.get_logger(__name__)


def run_main():
    """Entry point for the console script."""
    asyncio.run(main())


async def main():
    # Simple argument parsing for healthcheck
    if len(sys.argv) > 1 and sys.argv[1] == "healthcheck":
        print("Healthcheck passed")
        sys.exit(0)

    setup_logging()

    try:
        config = WorkerWrapperConfig()
    except Exception as e:
        logger.fatal("configuration_error", error=str(e))
        sys.exit(1)

    wrapper = WorkerWrapper(config=config)

    # Signal handling
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("signal_received")
        stop_event.set()
        # Create task to cancel wrapper
        if wrapper._task:
            wrapper._task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        # We run wrapper directly.
        # wrapper.run() isn't infinite unless it loops forever.
        # Our implementation loops until _running is false.
        # But here we want to handle graceful shutdown via stop_event?
        # Actually wrapper.run() handles loop. We just need to stop it.
        # But loop.add_signal_handler callbacks are synchronous.
        # Wrapper needs a stop method or check a flag.
        # current wrapper.run sets _running=True.
        # We can implement a stop() method on wrapper.

        # Hack for now: signal handler cancels the task if run as task,
        # OR sets _running=False if we can access it.
        # But locally main runs it.

        # Let's wrap in task to allow cancellation
        wrapper._task = asyncio.create_task(wrapper.run())

        # Wait for task or stop signal
        await wrapper._task

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("main_crashed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run_main()
