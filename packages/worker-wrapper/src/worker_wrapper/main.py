import asyncio
import signal
import sys

import structlog

from shared.log_config.config import setup_logging

from .config import WorkerWrapperConfig, validate_agent_config
from .wrapper import WorkerWrapper

logger = structlog.get_logger(__name__)


def run_main():
    """Entry point for the console script."""
    asyncio.run(main())


async def main():
    # Simple argument parsing for healthcheck
    # We check if "healthcheck" is anywhere in args to be robust against
    # entrypoint/command shifting.
    if "healthcheck" in sys.argv:
        print("Healthcheck passed")
        sys.exit(0)

    setup_logging()

    try:
        config = WorkerWrapperConfig()
        validate_agent_config(config)
    except Exception as e:
        logger.fatal("configuration_error", error=str(e))
        sys.exit(1)

    wrapper = WorkerWrapper(config=config)
    run_task = asyncio.create_task(wrapper.run())

    loop = asyncio.get_running_loop()

    def handle_signal():
        logger.info("signal_received")
        run_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await run_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("main_crashed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run_main()
