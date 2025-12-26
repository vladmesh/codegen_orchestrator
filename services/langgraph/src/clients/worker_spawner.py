"""Worker Spawner Client.

Client for requesting coding worker spawns via Redis pub/sub.
Used by LangGraph nodes to trigger container spawning.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
import uuid

from pydantic import ValidationError
import redis.asyncio as redis

from shared.logging_config import get_logger
from shared.schemas.worker_events import WorkerEventUnion, parse_worker_event
from src.config.settings import get_settings

logger = get_logger(__name__)

SPAWN_CHANNEL = "worker:spawn"
RESULT_CHANNEL = "worker:result"
EVENTS_CHANNEL_PREFIX = "worker:events"


@dataclass
class SpawnRequest:
    """Request to spawn a coding worker."""

    request_id: str
    repo: str
    github_token: str
    task_content: str
    task_title: str = "AI generated changes"
    model: str = "claude-sonnet-4-5-20250929"
    agents_content: str | None = None
    timeout_seconds: int = 600


@dataclass
class SpawnResult:
    """Result from a coding worker execution."""

    request_id: str
    success: bool
    exit_code: int
    output: str
    commit_sha: str | None = None
    branch: str | None = None
    files_changed: list[str] | None = None
    summary: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    logs_tail: str | None = None


async def request_spawn(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    model: str = "claude-sonnet-4-5-20250929",
    agents_content: str | None = None,
    timeout_seconds: int = 600,
) -> SpawnResult:
    """Request a coding worker spawn and wait for result.

    Publishes spawn request to Redis and waits for result.

    Args:
        repo: Repository in org/name format
        github_token: GitHub token for clone/push
        task_content: Task description for the AI agent
        task_title: Commit message title
        model: Factory.ai model to use
        agents_content: Custom AGENTS.md content
        timeout_seconds: Maximum wait time

    Returns:
        SpawnResult with execution details
    """
    request_id = str(uuid.uuid4())

    request = SpawnRequest(
        request_id=request_id,
        repo=repo,
        github_token=github_token,
        task_content=task_content,
        task_title=task_title,
        model=model,
        agents_content=agents_content,
        timeout_seconds=timeout_seconds,
    )

    settings = get_settings()
    redis_client = redis.from_url(settings.redis_url)
    pubsub = redis_client.pubsub()

    # Subscribe to result channel before publishing request
    result_channel = f"{RESULT_CHANNEL}:{request_id}"
    await pubsub.subscribe(result_channel)

    try:
        # Publish spawn request
        request_data = {
            "request_id": request.request_id,
            "repo": request.repo,
            "github_token": request.github_token,
            "task_content": request.task_content,
            "task_title": request.task_title,
            "model": request.model,
            "agents_content": request.agents_content,
            "timeout_seconds": request.timeout_seconds,
        }

        await redis_client.publish(SPAWN_CHANNEL, json.dumps(request_data))
        logger.info("worker_spawn_published", request_id=request_id, repo=repo)

        # Wait for result (timeout handled by spawner service)
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                return SpawnResult(**data)

        # Should not reach here
        return SpawnResult(
            request_id=request_id,
            success=False,
            exit_code=-1,
            output="No result received",
        )

    except TimeoutError:
        return SpawnResult(
            request_id=request_id,
            success=False,
            exit_code=-1,
            output=f"Timeout waiting for result after {timeout_seconds}s",
        )
    finally:
        await pubsub.unsubscribe(result_channel)
        await redis_client.aclose()


async def subscribe_worker_events(
    request_id: str,
    stop_on_terminal: bool = False,
) -> AsyncIterator[WorkerEventUnion]:
    """Subscribe to worker events for a specific request."""

    settings = get_settings()
    redis_client = redis.from_url(settings.redis_url)
    pubsub = redis_client.pubsub()
    channel = f"{EVENTS_CHANNEL_PREFIX}:{request_id}"
    await pubsub.subscribe(channel)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
            except json.JSONDecodeError:
                logger.warning("worker_event_invalid_json", request_id=request_id)
                continue

            try:
                event = parse_worker_event(data)
            except ValidationError as exc:
                logger.warning(
                    "worker_event_validation_failed",
                    request_id=request_id,
                    errors=exc.errors(),
                )
                continue

            yield event

            if stop_on_terminal and event.event_type in ("completed", "failed"):
                return
    finally:
        await pubsub.unsubscribe(channel)
        await redis_client.aclose()
