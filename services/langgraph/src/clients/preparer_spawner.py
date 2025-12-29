"""Preparer Spawner Client.

Client for requesting preparer container spawns via Redis pub/sub.
Used by LangGraph nodes to prepare project structure with service-template.
"""

from dataclasses import dataclass
import json
import uuid

import redis.asyncio as redis

from shared.logging_config import get_logger

from ..config.settings import get_settings

logger = get_logger(__name__)

PREPARER_SPAWN_CHANNEL = "preparer:spawn"
PREPARER_RESULT_CHANNEL = "preparer:result"


@dataclass
class PreparerRequest:
    """Request to spawn a preparer container."""

    request_id: str
    repo_url: str
    project_name: str
    modules: str  # comma-separated: "backend,tg_bot"
    github_token: str
    task_md: str = ""
    agents_md: str = ""
    service_template_ref: str = "main"
    timeout_seconds: int = 120


@dataclass
class PreparerResult:
    """Result from a preparer execution."""

    request_id: str
    success: bool
    exit_code: int
    output: str
    commit_sha: str | None = None
    error_message: str | None = None


async def request_preparer(
    repo_url: str,
    project_name: str,
    modules: list[str],
    github_token: str,
    task_md: str = "",
    agents_md: str = "",
    service_template_ref: str = "main",
    timeout_seconds: int = 120,
) -> PreparerResult:
    """Request a preparer container spawn and wait for result.

    Publishes spawn request to Redis and waits for result.

    Args:
        repo_url: Git repository URL (https://github.com/org/repo.git)
        project_name: Name of the project (snake_case)
        modules: List of modules to include (e.g., ["backend", "tg_bot"])
        github_token: GitHub token for clone/push
        task_md: Content for TASK.md file
        agents_md: Content for AGENTS.md file
        service_template_ref: Git ref for service-template (default: main)
        timeout_seconds: Maximum wait time (default: 120s)

    Returns:
        PreparerResult with execution details
    """
    request_id = str(uuid.uuid4())
    modules_str = ",".join(modules)

    request = PreparerRequest(
        request_id=request_id,
        repo_url=repo_url,
        project_name=project_name,
        modules=modules_str,
        github_token=github_token,
        task_md=task_md,
        agents_md=agents_md,
        service_template_ref=service_template_ref,
        timeout_seconds=timeout_seconds,
    )

    settings = get_settings()
    redis_client = redis.from_url(settings.redis_url)
    pubsub = redis_client.pubsub()

    # Subscribe to result channel before publishing request
    result_channel = f"{PREPARER_RESULT_CHANNEL}:{request_id}"
    await pubsub.subscribe(result_channel)

    try:
        # Publish spawn request
        request_data = {
            "request_id": request.request_id,
            "repo_url": request.repo_url,
            "project_name": request.project_name,
            "modules": request.modules,
            "github_token": request.github_token,
            "task_md": request.task_md,
            "agents_md": request.agents_md,
            "service_template_ref": request.service_template_ref,
            "timeout_seconds": request.timeout_seconds,
        }

        await redis_client.publish(PREPARER_SPAWN_CHANNEL, json.dumps(request_data))
        logger.info(
            "preparer_spawn_published",
            request_id=request_id,
            project_name=project_name,
            modules=modules_str,
        )

        # Wait for result
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                return PreparerResult(
                    request_id=data.get("request_id", request_id),
                    success=data.get("success", False),
                    exit_code=data.get("exit_code", -1),
                    output=data.get("output", ""),
                    commit_sha=data.get("commit_sha"),
                    error_message=data.get("error_message"),
                )

        # Should not reach here
        return PreparerResult(
            request_id=request_id,
            success=False,
            exit_code=-1,
            output="No result received",
        )

    except TimeoutError:
        return PreparerResult(
            request_id=request_id,
            success=False,
            exit_code=-1,
            output=f"Timeout waiting for result after {timeout_seconds}s",
        )
    except Exception as e:
        logger.error(
            "preparer_request_failed",
            request_id=request_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return PreparerResult(
            request_id=request_id,
            success=False,
            exit_code=-1,
            output=str(e),
            error_message=str(e),
        )
    finally:
        await pubsub.unsubscribe(result_channel)
        await redis_client.aclose()
