"""Worker Spawner Client for LangGraph.

Client for requesting coding worker spawns via Redis Stream (worker-manager service).
Used by LangGraph nodes to trigger container spawning.
"""

import asyncio
from dataclasses import dataclass
import json
import uuid

import redis.asyncio as redis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)
from shared.log_config import get_logger

from ..config.constants import Timeouts
from ..config.settings import get_settings

logger = get_logger(__name__)

COMMAND_STREAM = "worker:commands"
RESPONSE_STREAM = "worker:responses:developer"
CREATION_TIMEOUT = 60


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
    error_message: str | None = None
    logs_tail: str | None = None


async def _wait_for_response(
    redis_client: redis.Redis,
    group_name: str,
    consumer_id: str,
    request_id: str | None,
    timeout_s: float,
    stream: str = RESPONSE_STREAM,
) -> dict | None:
    """Wait for a specific response in the stream.

    If request_id is None, returns the first message (used for worker output streams).
    """
    start_time = asyncio.get_running_loop().time()

    while (asyncio.get_running_loop().time() - start_time) < timeout_s:
        try:
            messages = await redis_client.xreadgroup(
                groupname=group_name,
                consumername=consumer_id,
                streams={stream: ">"},
                count=1,
                block=1000,
            )
        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                # Group doesn't exist yet, create it
                try:
                    await redis_client.xgroup_create(stream, group_name, id="$", mkstream=True)
                except redis.ResponseError:
                    pass
                continue
            raise

        if messages:
            for _, stream_msgs in messages:
                for msg_id, msg_data in stream_msgs:
                    # Skip messages without 'data' field (wrong format)
                    if b"data" not in msg_data and "data" not in msg_data:
                        await redis_client.xack(stream, group_name, msg_id)
                        continue

                    data_str = msg_data[b"data"] if b"data" in msg_data else msg_data["data"]
                    try:
                        resp = json.loads(data_str)
                        # If no request_id filter, return any message
                        if request_id is None or resp.get("request_id") == request_id:
                            await redis_client.xack(stream, group_name, msg_id)
                            return resp
                        # ACK non-matching messages so they don't pile up
                        await redis_client.xack(stream, group_name, msg_id)
                    except json.JSONDecodeError:
                        await redis_client.xack(stream, group_name, msg_id)
                        continue
    return None


async def request_spawn(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    model: str = "claude-sonnet-4-5-20250929",
    agents_content: str | None = None,
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
) -> SpawnResult:
    """Request a coding worker spawn and wait for result.

    Uses worker-manager to create a developer worker container.
    """
    request_id = str(uuid.uuid4())
    settings = get_settings()

    redis_client = redis.from_url(settings.redis_url)

    # Unique consumer ID for this request listener
    consumer_id = f"langgraph-{request_id[:8]}"
    group_name = f"langgraph-client-{request_id[:8]}"

    worker_id = None

    try:
        # 1. Create consumer group for responses
        try:
            await redis_client.xgroup_create(RESPONSE_STREAM, group_name, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # 2. Build CreateWorkerCommand using shared contracts
        worker_name = f"dev-{repo.split('/')[-1][:20]}-{request_id[:8]}"

        # Load static developer instructions from INSTRUCTIONS.md
        from src.prompts import load_developer_instructions

        instructions = load_developer_instructions()
        if not instructions:
            instructions = "Read TASK.md for your implementation task."

        create_cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions=instructions,
                task_content=task_content,
                allowed_commands=["*"],
                capabilities=[WorkerCapability.GIT, WorkerCapability.GITHUB_CLI],
                env_vars={
                    "GITHUB_TOKEN": github_token,
                    "REPO_NAME": repo,
                },
            ),
            context={"source": "langgraph", "repo": repo},
        )

        await redis_client.xadd(COMMAND_STREAM, {"data": create_cmd.model_dump_json()})
        logger.info("worker_spawn_requested", request_id=request_id, repo=repo)

        # 3. Wait for Creation Response
        create_resp = await _wait_for_response(
            redis_client, group_name, consumer_id, request_id, CREATION_TIMEOUT
        )

        if not create_resp:
            return SpawnResult(request_id, False, -1, "Timeout waiting for container creation")

        if not create_resp.get("success"):
            return SpawnResult(
                request_id, False, -1, f"Creation failed: {create_resp.get('error')}"
            )

        worker_id = create_resp.get("worker_id")
        logger.info("worker_created", request_id=request_id, worker_id=worker_id)

        # 4. Set up output stream consumer group BEFORE sending task
        # Use id="0" to read any existing messages (in case worker is very fast)
        output_stream = f"worker:{worker_id}:output"
        try:
            await redis_client.xgroup_create(output_stream, group_name, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # 5. Send task message to worker input stream
        input_stream = f"worker:{worker_id}:input"
        task_message = {
            "request_id": request_id,
            "prompt": task_content,
            "user_id": 0,  # System task
        }
        await redis_client.xadd(input_stream, {"data": json.dumps(task_message)})
        logger.info("task_sent_to_worker", request_id=request_id, worker_id=worker_id)

        # Wait for output (worker output doesn't have request_id, so pass None)
        output_resp = await _wait_for_response(
            redis_client, group_name, consumer_id, None, float(timeout_seconds), output_stream
        )

        if output_resp:
            # Worker outputs: {"content": "...", "status": "success|failed"}
            is_success = output_resp.get("status") == "success" or output_resp.get("success", False)
            content = output_resp.get(
                "content", output_resp.get("response", output_resp.get("output", ""))
            )
            return SpawnResult(
                request_id=request_id,
                success=is_success,
                exit_code=0 if is_success else 1,
                output=content,
                commit_sha=output_resp.get("commit_sha"),
                branch=output_resp.get("branch"),
                files_changed=output_resp.get("files_changed"),
                error_message=output_resp.get("error"),
            )
        else:
            # Timeout - cleanup the zombie container
            if worker_id:
                logger.warning(
                    "worker_timeout_cleanup",
                    worker_id=worker_id,
                    timeout_seconds=timeout_seconds,
                )
                delete_cmd = DeleteWorkerCommand(
                    request_id=f"cleanup-{request_id}",
                    worker_id=worker_id,
                )
                await redis_client.xadd(COMMAND_STREAM, {"data": delete_cmd.model_dump_json()})

            return SpawnResult(
                request_id,
                False,
                -1,
                f"Timeout after {timeout_seconds}s waiting for worker output. "
                "Container cleaned up.",
                error_message="execution_timeout",
            )

    except Exception as e:
        logger.error("spawn_failed", error=str(e))
        return SpawnResult(request_id, False, -1, str(e))
    finally:
        # Cleanup consumer groups (ignore errors - groups may not exist)
        try:
            await redis_client.xgroup_destroy(RESPONSE_STREAM, group_name)
        except Exception as e:
            logger.debug("cleanup_response_group_failed", error=str(e))
        if worker_id:
            try:
                await redis_client.xgroup_destroy(f"worker:{worker_id}:output", group_name)
            except Exception as e:
                logger.debug("cleanup_output_group_failed", error=str(e))
        await redis_client.aclose()
