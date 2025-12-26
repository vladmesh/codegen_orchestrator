"""Worker Spawner Service.

Listens to Redis for spawn requests and creates Docker containers
for AI coding tasks (Factory.ai Droid).
"""

import asyncio
import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging
from shared.schemas.worker_events import WorkerCompleted, WorkerFailed

from src.config import get_settings
from src.event_listener import wait_for_terminal_event

logger = structlog.get_logger()

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


def result_from_event(event: WorkerCompleted | WorkerFailed) -> SpawnResult:
    """Convert a worker event into a spawn result."""

    if isinstance(event, WorkerCompleted):
        return SpawnResult(
            request_id=event.request_id,
            success=True,
            exit_code=0,
            output=event.summary,
            commit_sha=event.commit_sha,
            branch=event.branch,
            files_changed=event.files_changed,
            summary=event.summary,
        )

    output = event.error_message
    if event.logs_tail:
        output = f"{event.error_message}\n\n{event.logs_tail}"
    return SpawnResult(
        request_id=event.request_id,
        success=False,
        exit_code=1,
        output=output,
        error_type=event.error_type,
        error_message=event.error_message,
        logs_tail=event.logs_tail,
    )


async def spawn_container(
    request: SpawnRequest,
    redis_url: str,
    events_channel: str,
) -> SpawnResult:
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
        "-e",
        f"ORCHESTRATOR_REDIS_URL={redis_url}",
        "-e",
        f"ORCHESTRATOR_REQUEST_ID={request.request_id}",
        "-e",
        f"ORCHESTRATOR_EVENTS_CHANNEL={events_channel}",
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


async def handle_request(
    redis_client: redis.Redis,
    message: dict[str, Any],
    redis_url: str,
) -> None:
    """Handle a spawn request from Redis."""
    pubsub = None
    events_channel = None
    try:
        data = json.loads(message["data"])
        request = SpawnRequest(**data)

        logger.info("spawn_request_received", request_id=request.request_id, repo=request.repo)

        events_channel = f"{EVENTS_CHANNEL_PREFIX}:{request.request_id}"
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(events_channel)

        spawn_task = asyncio.create_task(
            spawn_container(request, redis_url, events_channel),
        )
        event_task = asyncio.create_task(wait_for_terminal_event(pubsub))

        done, _pending = await asyncio.wait(
            {spawn_task, event_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        event = None
        if event_task in done:
            event = event_task.result()
            spawn_result = await spawn_task
        else:
            spawn_result = spawn_task.result()
            try:
                event = await asyncio.wait_for(event_task, timeout=5)
            except TimeoutError:
                event = None

        if event_task and not event_task.done():
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task

        if isinstance(event, (WorkerCompleted, WorkerFailed)):
            result = result_from_event(event)
        else:
            result = spawn_result

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
    finally:
        if pubsub and events_channel:
            await pubsub.unsubscribe(events_channel)
            close_result = pubsub.close()
            if asyncio.iscoroutine(close_result):
                await close_result


async def main() -> None:
    """Main loop - listen for spawn requests."""
    setup_logging(service_name="worker_spawner")
    settings = get_settings()
    logger.info("worker_spawner_starting", redis_url=settings.redis_url)

    redis_client = redis.from_url(settings.redis_url)
    pubsub = redis_client.pubsub()

    await pubsub.subscribe(SPAWN_CHANNEL)
    logger.info("redis_channel_subscribed", channel=SPAWN_CHANNEL)

    async for message in pubsub.listen():
        if message["type"] == "message":
            # Handle request in background to not block listener
            asyncio.create_task(handle_request(redis_client, message, settings.redis_url))


if __name__ == "__main__":
    asyncio.run(main())
