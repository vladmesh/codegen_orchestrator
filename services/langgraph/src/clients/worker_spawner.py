"""Worker Spawner Client.

Client for requesting coding worker spawns via Redis pub/sub.
Used by LangGraph nodes to trigger container spawning.
"""

from dataclasses import dataclass
import json
import logging
import os
import uuid

import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
SPAWN_CHANNEL = "worker:spawn"
RESULT_CHANNEL = "worker:result"


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
    
    redis_client = redis.from_url(REDIS_URL)
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
        logger.info(f"Published spawn request: {request_id}")
        
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
