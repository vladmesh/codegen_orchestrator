"""Worker Spawner Service.

Listens to Redis for spawn requests and creates Docker containers
for AI coding tasks (Factory.ai Droid).
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis

logging.basicConfig(level=logging.INFO)
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


async def spawn_container(request: SpawnRequest) -> SpawnResult:
    """Spawn a Docker container to run the coding task."""
    factory_api_key = os.getenv("FACTORY_AI_API_KEY")
    if not factory_api_key:
        return SpawnResult(
            request_id=request.request_id,
            success=False,
            exit_code=-1,
            output="FACTORY_AI_API_KEY not set",
        )

    # Build docker run command
    cmd = [
        "docker",
        "run",
        "--rm",
        "--runtime=sysbox-runc",  # Enable Docker-in-Docker via Sysbox
        "-e", f"GITHUB_TOKEN={request.github_token}",
        "-e", f"FACTORY_API_KEY={factory_api_key}",
        "-e", f"REPO={request.repo}",
        "-e", f"TASK_CONTENT={request.task_content}",
        "-e", f"TASK_TITLE={request.task_title}",
        "-e", f"MODEL={request.model}",
    ]

    if request.agents_content:
        cmd.extend(["-e", f"AGENTS_CONTENT={request.agents_content}"])

    cmd.append("coding-worker:latest")

    logger.info(f"Spawning worker for request {request.request_id}: {request.repo}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_seconds
            )
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
        logger.info(
            f"Worker {request.request_id} completed: "
            f"success={success}, commit={commit_sha}"
        )

        return SpawnResult(
            request_id=request.request_id,
            success=success,
            exit_code=proc.returncode or 0,
            output=output,
            commit_sha=commit_sha,
        )

    except Exception as e:
        logger.exception(f"Failed to spawn worker: {e}")
        return SpawnResult(
            request_id=request.request_id,
            success=False,
            exit_code=-1,
            output=str(e),
        )


async def handle_request(
    redis_client: redis.Redis, message: dict[str, Any]
) -> None:
    """Handle a spawn request from Redis."""
    try:
        data = json.loads(message["data"])
        request = SpawnRequest(**data)

        logger.info(f"Received spawn request: {request.request_id}")

        # Spawn the container
        result = await spawn_container(request)

        # Publish result back to Redis
        result_data = asdict(result)
        await redis_client.publish(
            f"{RESULT_CHANNEL}:{request.request_id}",
            json.dumps(result_data),
        )

        logger.info(f"Published result for {request.request_id}")

    except Exception as e:
        logger.exception(f"Error handling request: {e}")


async def main() -> None:
    """Main loop - listen for spawn requests."""
    logger.info(f"Worker Spawner starting, connecting to {REDIS_URL}")

    redis_client = redis.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()

    await pubsub.subscribe(SPAWN_CHANNEL)
    logger.info(f"Subscribed to {SPAWN_CHANNEL}")

    async for message in pubsub.listen():
        if message["type"] == "message":
            # Handle request in background to not block listener
            asyncio.create_task(handle_request(redis_client, message))


if __name__ == "__main__":
    asyncio.run(main())
