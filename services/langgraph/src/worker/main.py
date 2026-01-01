"""LangGraph worker main orchestration.

After CLI Agent migration (Phase 8), this worker handles:
- Provisioner triggers (server provisioning)
- Worker events (engineering/deploy queue triggers)

User messages are handled by agent-spawner, not this worker.
"""

import asyncio

from shared.logging_config import setup_logging

from .events import listen_worker_events
from .provisioner import listen_provisioner_triggers


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    await asyncio.gather(
        listen_provisioner_triggers(),
        listen_worker_events(),
    )


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
