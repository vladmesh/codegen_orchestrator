"""Worker Spawner Client for LangGraph.

Client for requesting coding worker spawns via Redis Stream (worker-manager service).
Used by LangGraph nodes to trigger container spawning.
"""

import asyncio
from dataclasses import dataclass
import json
import uuid

import redis.asyncio as redis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)
from shared.log_config import get_logger
from shared.queues import WORKER_COMMANDS, WORKER_RESPONSES

from ..config.constants import Timeouts
from ..config.settings import get_settings

logger = get_logger(__name__)
CREATION_TIMEOUT = 60
READY_POLL_TIMEOUT = 300  # Max wait for image build + container start
READY_POLL_INTERVAL = 2  # Poll interval for worker status


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
    worker_id: str | None = None
    gave_up_reason: str | None = None
    worker_report: str | None = None


LIVENESS_CHECK_INTERVAL_S = 30  # Check worker liveness every 30 seconds


async def _wait_until_ready(
    redis_client: redis.Redis,
    worker_id: str,
    request_id: str,
    timeout: float = READY_POLL_TIMEOUT,
) -> SpawnResult | None:
    """Poll worker:status until RUNNING. Returns SpawnResult on failure, None on success."""
    start = asyncio.get_running_loop().time()
    seen_status = False
    while (asyncio.get_running_loop().time() - start) < timeout:
        status = await redis_client.hget(f"worker:status:{worker_id}", "status")
        status_str = status.decode() if isinstance(status, bytes) else status
        if status_str == WorkerStatus.RUNNING:
            return None
        if status_str == WorkerStatus.FAILED:
            error = await redis_client.get(f"worker:error:{worker_id}")
            error_msg = error.decode() if isinstance(error, bytes) else str(error)
            return SpawnResult(request_id, False, -1, f"Creation failed: {error_msg}")
        if status_str is None:
            if seen_status:
                return SpawnResult(request_id, False, -1, "Worker disappeared during creation")
        else:
            seen_status = True
        await asyncio.sleep(READY_POLL_INTERVAL)
    # Timeout — caller should cleanup
    logger.warning("worker_ready_timeout", worker_id=worker_id, timeout_seconds=timeout)
    delete_cmd = DeleteWorkerCommand(
        request_id=f"cleanup-{request_id}",
        worker_id=worker_id,
        reason="timeout",
    )
    await redis_client.xadd(WORKER_COMMANDS, {"data": delete_cmd.model_dump_json()})
    return SpawnResult(
        request_id,
        False,
        -1,
        f"Timeout after {timeout}s waiting for worker to become ready",
    )


async def _check_worker_alive(redis_client: redis.Redis, worker_id: str) -> bool:
    """Check if a worker is still alive by reading its Redis status.

    Returns False if the status is DEAD (set by DockerEventsListener when the
    container dies) or if the status key has been deleted entirely.
    """
    status = await redis_client.hget(f"worker:status:{worker_id}", "status")
    if status is None:
        # Key deleted — worker was cleaned up
        return False
    # Handle both bytes and str (depends on decode_responses setting)
    status_str = status.decode() if isinstance(status, bytes) else status
    if status_str == WorkerStatus.DEAD:
        return False
    return True


async def _wait_for_response(
    redis_client: redis.Redis,
    group_name: str,
    consumer_id: str,
    request_id: str | None,
    timeout_s: float,
    stream: str = WORKER_RESPONSES,
    worker_id: str | None = None,
) -> dict | None:
    """Wait for a specific response in the stream.

    If request_id is None, returns the first message (used for worker output streams).
    If worker_id is provided, periodically checks that the worker container is still
    alive. Returns None immediately if the worker is detected as dead.
    """
    start_time = asyncio.get_running_loop().time()
    last_liveness_check = start_time

    while (asyncio.get_running_loop().time() - start_time) < timeout_s:
        # Periodic liveness check (every LIVENESS_CHECK_INTERVAL_S seconds)
        now = asyncio.get_running_loop().time()
        if worker_id and (now - last_liveness_check) >= LIVENESS_CHECK_INTERVAL_S:
            last_liveness_check = now
            try:
                if not await _check_worker_alive(redis_client, worker_id):
                    logger.warning(
                        "worker_dead_detected",
                        worker_id=worker_id,
                        elapsed_s=round(now - start_time, 1),
                    )
                    return None
            except Exception as e:
                # Don't fail the whole wait on a check error
                logger.debug("liveness_check_error", worker_id=worker_id, error=str(e))

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
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
    project_id: str | None = None,
    repo_id: str | None = None,
    agent_type: AgentType = AgentType.CLAUDE,
    story_md: str | None = None,
    branch: str | None = None,
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
            await redis_client.xgroup_create(WORKER_RESPONSES, group_name, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # 2. Build CreateWorkerCommand using shared contracts
        worker_name = f"dev-{repo.split('/')[-1][:20]}-{request_id[:8]}"

        # Load static developer instructions from INSTRUCTIONS.md
        from src.prompts import load_developer_instructions

        instructions = load_developer_instructions()
        if not instructions:
            instructions = "Read /workspace/TASK.md for your implementation task."

        create_cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=agent_type,
                instructions=instructions,
                task_content=task_content,
                allowed_commands=["*"],
                capabilities=[WorkerCapability.GIT, WorkerCapability.GITHUB_CLI],
                env_vars={
                    "GITHUB_TOKEN": github_token,
                    "REPO_NAME": repo,
                },
                project_id=project_id,
                repo_id=repo_id,
                branch=branch,
            ),
            context={"source": "langgraph", "repo": repo, "project_id": project_id or ""},
        )

        await redis_client.xadd(WORKER_COMMANDS, {"data": create_cmd.model_dump_json()})
        logger.info("worker_spawn_requested", request_id=request_id, repo=repo)

        # 3. Wait for early ACK (worker_id) — should be near-instant
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
        logger.info("worker_ack_received", request_id=request_id, worker_id=worker_id)

        # 3b. Poll worker status until RUNNING (image build + container start)
        ready_failure = await _wait_until_ready(redis_client, worker_id, request_id)
        if ready_failure:
            return ready_failure

        logger.info("worker_ready", request_id=request_id, worker_id=worker_id)

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
        if story_md:
            task_message["story_md"] = story_md
        await redis_client.xadd(input_stream, {"data": json.dumps(task_message)})
        logger.info("task_sent_to_worker", request_id=request_id, worker_id=worker_id)

        # Wait for output (worker output doesn't have request_id, so pass None)
        output_resp = await _wait_for_response(
            redis_client,
            group_name,
            consumer_id,
            None,
            float(timeout_seconds),
            output_stream,
            worker_id=worker_id,
        )

        if output_resp:
            # Worker outputs: {"content": "...", "status": "success|failed|rejected|blocked"}
            status = output_resp.get("status", "")
            is_success = status in ("success", "completed") or output_resp.get("success", False)
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
                worker_id=worker_id,
                gave_up_reason=(
                    output_resp.get("block_reason") or output_resp.get("reject_reason")
                ),
                worker_report=output_resp.get("worker_report"),
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
                    reason="timeout",
                )
                await redis_client.xadd(WORKER_COMMANDS, {"data": delete_cmd.model_dump_json()})

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
            await redis_client.xgroup_destroy(WORKER_RESPONSES, group_name)
        except Exception as e:
            logger.debug("cleanup_response_group_failed", error=str(e))
        if worker_id:
            try:
                await redis_client.xgroup_destroy(f"worker:{worker_id}:output", group_name)
            except Exception as e:
                logger.debug("cleanup_output_group_failed", error=str(e))
        await redis_client.aclose()


async def send_task_to_worker(
    worker_id: str,
    task_content: str,
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
    *,
    clear_session: bool = False,
    story_md: str | None = None,
    branch: str | None = None,
) -> SpawnResult:
    """Send a new task to an existing worker and wait for output.

    Unlike request_spawn(), this does NOT create a new container.
    It sends a prompt to the worker's input stream and waits for output.

    Args:
        clear_session: If True, worker clears its session before executing,
            forcing a fresh Claude CLI session (no --resume). Use for retries
            to avoid inheriting errors from a failed previous attempt.
    """
    request_id = str(uuid.uuid4())
    settings = get_settings()
    redis_client = redis.from_url(settings.redis_url)

    consumer_id = f"langgraph-reuse-{request_id[:8]}"
    group_name = f"langgraph-reuse-{request_id[:8]}"

    input_stream = f"worker:{worker_id}:input"
    output_stream = f"worker:{worker_id}:output"

    try:
        # 1. Set up output stream consumer group BEFORE sending task
        try:
            await redis_client.xgroup_create(output_stream, group_name, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # 2. Send task to worker input stream
        task_message = {
            "request_id": request_id,
            "prompt": task_content,
        }
        if clear_session:
            task_message["clear_session"] = True
        if story_md:
            task_message["story_md"] = story_md
        if branch:
            task_message["branch"] = branch
        await redis_client.xadd(input_stream, {"data": json.dumps(task_message)})
        logger.info(
            "task_sent_to_existing_worker",
            request_id=request_id,
            worker_id=worker_id,
        )

        # 3. Wait for output
        output_resp = await _wait_for_response(
            redis_client,
            group_name,
            consumer_id,
            None,
            float(timeout_seconds),
            output_stream,
            worker_id=worker_id,
        )

        if output_resp:
            status = output_resp.get("status", "")
            is_success = status in ("success", "completed") or output_resp.get("success", False)
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
                worker_id=worker_id,
                gave_up_reason=(
                    output_resp.get("block_reason") or output_resp.get("reject_reason")
                ),
                worker_report=output_resp.get("worker_report"),
            )
        else:
            return SpawnResult(
                request_id=request_id,
                success=False,
                exit_code=-1,
                output=f"Timeout after {timeout_seconds}s waiting for worker output.",
                error_message="execution_timeout",
                worker_id=worker_id,
            )

    except Exception as e:
        logger.error("send_task_failed", error=str(e), worker_id=worker_id)
        return SpawnResult(request_id, False, -1, str(e), worker_id=worker_id)
    finally:
        try:
            await redis_client.xgroup_destroy(output_stream, group_name)
        except Exception as e:
            logger.debug("cleanup_output_group_failed", error=str(e))
        await redis_client.aclose()


async def delete_worker(
    worker_id: str,
    reason: str | None = None,
) -> None:
    """Send DeleteWorkerCommand for a worker."""
    settings = get_settings()
    redis_client = redis.from_url(settings.redis_url)

    try:
        delete_cmd = DeleteWorkerCommand(
            request_id=f"delete-{worker_id}",
            worker_id=worker_id,
            reason=reason,
        )
        await redis_client.xadd(WORKER_COMMANDS, {"data": delete_cmd.model_dump_json()})
        logger.info("worker_delete_requested", worker_id=worker_id)
    finally:
        await redis_client.aclose()
