"""LangGraph service main orchestration.

Handles:
- Provisioner triggers (server provisioning)
- Worker events (engineering/deploy queue triggers)
- PO ReactAgent consumer (unless no channel of its LLM channel chain is configured)

Note: Engineering and Deploy queues are consumed by dedicated consumers:
- engineering-worker (services/langgraph/src/consumers/engineering.py)
- deploy-worker (services/langgraph/src/consumers/deploy.py)
"""

import asyncio

import structlog

from shared.log_config import setup_logging
from shared.queues import PO_INPUT_QUEUE

from .clients.api import api_client
from .config.settings import get_settings
from .llm import (
    LLMAgent,
    build_agent_llm,
    load_channel_chain,
    log_channel_readiness,
    unconfigured_channel_env,
)
from .provisioner import listen_provisioner_triggers
from .worker_events import listen_worker_events

logger = structlog.get_logger(__name__)


async def _po_missing_env() -> list[str]:
    """LLM env the PO cannot run without: none once a channel of each of its chains is configured.

    Reads both PO chains from agent configuration, so an invalid stored chain
    stops the service here with `InvalidChannelChainError`, and logs each
    channel's readiness (`llm_channel_ready`).
    """
    settings = get_settings()
    missing: list[str] = []
    for agent in (LLMAgent.PO, LLMAgent.PO_SUMMARIZER):
        chain = await load_channel_chain(api_client, agent)
        await log_channel_readiness(build_agent_llm(agent, chain, settings))
        missing += unconfigured_channel_env(agent, chain, settings)
    return list(dict.fromkeys(missing))


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    po_missing = await _po_missing_env()
    summarization_config = None
    po_llms = None
    if not po_missing:
        settings = get_settings()
        if not settings.checkpoint_database_url:
            raise RuntimeError(
                "CHECKPOINT_DATABASE_URL is required when the PO consumer is enabled; "
                "refusing to start with non-durable conversation state"
            )

        # Validate every PO-only startup dependency before unrelated background
        # loops begin. Operational summarization tuning is required system config;
        # an unavailable/malformed source is not an env-fallback mode.
        from .consumers.po import load_po_llms, load_summarization_config

        summarization_config = load_summarization_config(settings.api_base_url)
        po_llms = await load_po_llms(settings)

    tasks = [
        listen_provisioner_triggers(),
        listen_worker_events(),
    ]

    if po_missing:
        logger.error(
            "po_consumer_disabled",
            missing_env=po_missing,
            impact=f"{PO_INPUT_QUEUE} is not consumed, user messages stay unanswered",
            fix="set these vars in .env (see .env.example) and restart langgraph",
        )
    else:
        from shared.redis import RedisStreamClient

        from .agents.po.reminders import run_reminder_poller
        from .consumers.po import run_po_consumer

        poller_client = RedisStreamClient(redis_url=settings.redis_url)
        await poller_client.connect()

        logger.info("po_consumer_enabled")
        tasks.append(run_po_consumer(summarization_config=summarization_config, llms=po_llms))
        tasks.append(run_reminder_poller(poller_client))

    await asyncio.gather(*tasks)


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
