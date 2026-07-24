"""LangGraph service main orchestration.

Handles:
- Provisioner triggers (server provisioning)
- Worker events (engineering/deploy queue triggers)
- PO ReactAgent consumer (if PO_LLM_* env vars are set)

Note: Engineering and Deploy queues are consumed by dedicated consumers:
- engineering-worker (services/langgraph/src/consumers/engineering.py)
- deploy-worker (services/langgraph/src/consumers/deploy.py)
"""

import asyncio

import structlog

from shared.log_config import setup_logging

from .config.agent_llm_env import missing_llm_env
from .config.settings import get_settings
from .provisioner import listen_provisioner_triggers
from .worker_events import listen_worker_events

logger = structlog.get_logger(__name__)


def _po_missing_env() -> list[str]:
    """PO ReactAgent env vars that are not configured."""
    return missing_llm_env("po", get_settings())


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    tasks = [
        listen_provisioner_triggers(),
        listen_worker_events(),
    ]

    po_missing = _po_missing_env()
    if po_missing:
        logger.error(
            "po_consumer_disabled",
            missing_env=po_missing,
            impact="po:queue is not consumed, user messages stay unanswered",
            fix="set these vars in .env (see .env.example) and restart langgraph",
        )
    else:
        from shared.redis_client import RedisStreamClient

        from .agents.po.reminders import run_reminder_poller
        from .consumers.po import run_po_consumer

        settings = get_settings()
        poller_client = RedisStreamClient(redis_url=settings.redis_url)
        await poller_client.connect()

        logger.info("po_consumer_enabled")
        tasks.append(run_po_consumer())
        tasks.append(run_reminder_poller(poller_client))

    await asyncio.gather(*tasks)


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
