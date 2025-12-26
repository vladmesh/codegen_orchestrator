"""Worker Spawner Service.

Listens to Redis for spawn requests and creates Docker containers
for AI coding tasks (Factory.ai Droid).
"""

import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging

logger = structlog.get_logger()

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


async def spawn_container(request: SpawnRequest) -> SpawnResult:
    """Spawn a Docker container to run the coding task."""
    factory_api_key = os.getenv("FACTORY_AI_API_KEY")
    if not factory_api_key:
        logger.error("factory_ai_api_key_missing", request_id=request.request_id)
        return SpawnResult(
            request_id=request.request_id,
            success=False,
            exit_code=-1,
            output="FACTORY_AI_API_KEY not set",
        )

    # Build docker run command
    cid_file = tempfile.NamedTemporaryFile(delete=False)
    cid_path = cid_file.name
    cid_file.close()

    cmd = [
        "docker",
        "run",
        "--rm",
        "--runtime=sysbox-runc",  # Enable Docker-in-Docker via Sysbox
        "--cidfile",
        cid_path,
        "-e",
        f"GITHUB_TOKEN={request.github_token}",
        "-e",
        f"FACTORY_API_KEY={factory_api_key}",
        "-e",
        f"REPO={request.repo}",
        "-e",
        f"TASK_CONTENT={request.task_content}",
        "-e",
        f"TASK_TITLE={request.task_title}",
        "-e",
        f"MODEL={request.model}",
    ]

    if request.agents_content:
        cmd.extend(["-e", f"AGENTS_CONTENT={request.agents_content}"])

    image_name = "coding-worker:latest"
    cmd.append(image_name)

    logger.info(
        "docker_container_creating",
        request_id=request.request_id,
        repo=request.repo,
        image=image_name,
    )
    start_time = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        container_id = None
        try:
            with open(cid_path) as handle:
                container_id = handle.read().strip() or None
        except FileNotFoundError:
            container_id = None

        if container_id:
            logger.info(
                "docker_container_created",
                request_id=request.request_id,
                container_id=container_id[:12],
            )

        logger.info(
            "docker_container_running",
            request_id=request.request_id,
            container_id=container_id[:12] if container_id else None,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=request.timeout_seconds)
            output = stdout.decode() if stdout else ""
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SpawnResult(
                request_id=request.request_id,
                success=False,
                exit_code=-1,
                output=f"Timeout after {request.timeout_seconds}s",
            )

        # Parse output for commit SHA
        commit_sha = None
        for line in output.split("\n"):
            if line.startswith("Pushed commit:"):
                commit_sha = line.split(":")[-1].strip()
                break

        success = proc.returncode == 0
        duration = time.time() - start_time
        logger.info(
            "worker_execution_complete",
            request_id=request.request_id,
            exit_code=proc.returncode or 0,
            duration_sec=round(duration, 2),
            success=success,
            commit_sha=commit_sha,
        )

        return SpawnResult(
            request_id=request.request_id,
            success=success,
            exit_code=proc.returncode or 0,
            output=output,
            commit_sha=commit_sha,
        )

    except Exception as e:
        logger.error(
            "worker_spawn_failed",
            request_id=request.request_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return SpawnResult(
            request_id=request.request_id,
            success=False,
            exit_code=-1,
            output=str(e),
        )
    finally:
        if os.path.exists(cid_path):
            os.remove(cid_path)


async def handle_request(redis_client: redis.Redis, message: dict[str, Any]) -> None:
    """Handle a spawn request from Redis."""
    try:
        data = json.loads(message["data"])
        request = SpawnRequest(**data)

        logger.info("spawn_request_received", request_id=request.request_id, repo=request.repo)

        # Spawn the container
        result = await spawn_container(request)

        # Publish result back to Redis
        result_data = asdict(result)
        await redis_client.publish(
            f"{RESULT_CHANNEL}:{request.request_id}",
            json.dumps(result_data),
        )

        logger.info(
            "spawn_result_published",
            request_id=request.request_id,
            channel=f"{RESULT_CHANNEL}:{request.request_id}",
        )

    except Exception as e:
        logger.error(
            "spawn_request_handling_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


async def main() -> None:
    """Main loop - listen for spawn requests."""
    setup_logging(service_name="worker_spawner")
    logger.info("worker_spawner_starting", redis_url=REDIS_URL)

    redis_client = redis.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()

    await pubsub.subscribe(SPAWN_CHANNEL)
    logger.info("redis_channel_subscribed", channel=SPAWN_CHANNEL)

    async for message in pubsub.listen():
        if message["type"] == "message":
            # Handle request in background to not block listener
            asyncio.create_task(handle_request(redis_client, message))


if __name__ == "__main__":
    asyncio.run(main())
