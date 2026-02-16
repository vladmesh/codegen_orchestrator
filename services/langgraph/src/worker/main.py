"""LangGraph worker main orchestration.

After CLI Agent migration (Phase 8), this worker handles:
- Provisioner triggers (server provisioning)
- Worker events (engineering/deploy queue triggers)
- PO ReactAgent consumer (if PO_LLM_* env vars are set)

Note: Engineering and Deploy queues are consumed by dedicated workers:
- engineering-worker (services/langgraph/src/workers/engineering_worker.py)
- deploy-worker (services/langgraph/src/workers/deploy_worker.py)
"""

import asyncio

import structlog

from shared.log_config import setup_logging

from ..config.settings import get_settings
from .events import listen_worker_events
from .provisioner import listen_provisioner_triggers

logger = structlog.get_logger(__name__)


def _po_enabled() -> bool:
    """Check if PO ReactAgent env vars are configured."""
    settings = get_settings()
    return all([settings.po_llm_model, settings.po_llm_base_url, settings.po_llm_api_key])


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    tasks = [
        listen_provisioner_triggers(),
        listen_worker_events(),
    ]

    if _po_enabled():
        import redis.asyncio as aioredis

        from ..po.consumer import run_po_consumer
        from ..po.reminders import run_reminder_poller

        settings = get_settings()
        poller_redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        logger.info("po_consumer_enabled")
        tasks.append(run_po_consumer())
        tasks.append(run_reminder_poller(poller_redis))
    else:
        logger.info("po_consumer_disabled", reason="PO_LLM_* env vars not set")

    await asyncio.gather(*tasks)


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
