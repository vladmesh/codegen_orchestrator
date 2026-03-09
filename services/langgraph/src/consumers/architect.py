"""Architect consumer — consumes from architect:queue and decomposes stories into tasks.

Run standalone: python -m src.consumers.architect
"""

from __future__ import annotations

import uuid

from pydantic import ValidationError
import structlog

from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_GROUP, ARCHITECT_QUEUE
from shared.redis_client import RedisStreamClient

from ..agents.architect.graph import create_architect_graph
from ..config.settings import get_settings
from ._base import start_worker

logger = structlog.get_logger(__name__)


async def process_architect_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single architect job by running the Architect ReAct agent.

    Args:
        job_data: Job data from Redis queue (story_id, project_id, user_id).
        redis: Redis client (unused but required by base worker signature).

    Returns:
        Result dict with status and details.
    """
    try:
        msg = ArchitectMessage.model_validate(job_data)
    except ValidationError:
        logger.warning("architect_invalid_message", data=job_data)
        return {"status": "skipped", "error": "invalid message"}

    log = logger.bind(story_id=msg.story_id, project_id=msg.project_id)
    log.info("architect_job_started")

    settings = get_settings()

    if not settings.architect_llm_api_key:
        log.error("architect_llm_not_configured")
        return {"status": "failed", "error": "ARCHITECT_LLM_API_KEY not set"}

    try:
        graph = create_architect_graph(
            model=settings.architect_llm_model or "anthropic/claude-sonnet-4",
            base_url=settings.architect_llm_base_url or "https://openrouter.ai/api/v1",
            api_key=settings.architect_llm_api_key,
        )

        initial_state = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Decompose story {msg.story_id} for project {msg.project_id}. "
                        f"Start by calling get_story and get_project_spec."
                    ),
                }
            ],
            "story_id": msg.story_id,
            "project_id": msg.project_id,
            "user_id": msg.user_id,
        }

        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await graph.ainvoke(initial_state, config=config)

        log.info(
            "architect_job_success",
            message_count=len(result.get("messages", [])),
        )
        return {"status": "success"}

    except Exception as e:
        log.error(
            "architect_job_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {"status": "failed", "error": str(e)}


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="architect",
        queue=ARCHITECT_QUEUE,
        process_fn=process_architect_job,
        group=ARCHITECT_GROUP,
    )


if __name__ == "__main__":
    main()
