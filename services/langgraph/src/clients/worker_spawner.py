"""Worker Spawner Client for LangGraph.

Client for requesting coding worker spawns via Redis Stream (workers-spawner service).
Used by LangGraph nodes to trigger container spawning.
"""

import asyncio
from dataclasses import dataclass
import json
import uuid

import redis.asyncio as redis

from shared.logging_config import get_logger

from ..config.constants import Timeouts
from ..config.settings import get_settings

logger = get_logger(__name__)

COMMAND_STREAM = "cli-agent:commands"
RESPONSE_STREAM = "cli-agent:responses"
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
    request_id: str,
    timeout_s: float,
) -> dict | None:
    """Wait for a specific response in the stream."""
    start_time = asyncio.get_running_loop().time()

    while (asyncio.get_running_loop().time() - start_time) < timeout_s:
        messages = await redis_client.xreadgroup(
            groupname=group_name,
            consumername=consumer_id,
            streams={RESPONSE_STREAM: ">"},
            count=1,
            block=1000,
        )

        if messages:
            for _, stream_msgs in messages:
                for msg_id, msg_data in stream_msgs:
                    data_str = msg_data[b"data"] if b"data" in msg_data else msg_data["data"]
                    try:
                        resp = json.loads(data_str)
                        if resp.get("request_id") == request_id:
                            await redis_client.xack(RESPONSE_STREAM, group_name, msg_id)
                            return resp
                    except json.JSONDecodeError:
                        continue
    return None


async def request_spawn(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    model: str = "claude-sonnet-4-5-20250929",  # Used to select agent model if configurable
    agents_content: str | None = None,
    timeout_seconds: int = Timeouts.WORKER_SPAWN,
) -> SpawnResult:
    """Request a coding worker spawn and wait for result.

    Uses 'factory-droid' agent type via workers-spawner service.
    """
    request_id = str(uuid.uuid4())
    settings = get_settings()

    # Construct WorkerConfig
    # Use 'claude-code' agent directly for now as factory-droid is a stub.
    config = {
        "name": f"Dev {repo.split('/')[-1]}",
        "agent": "claude-code",
        "capabilities": ["git", "github", "copier", "node", "python"],
        "allowed_tools": ["project"],
        "env_vars": {
            "GITHUB_TOKEN": github_token,
            "REPO_NAME": repo,
            # Task content is sent via file to avoid shell quoting issues with large prompts
        },
        "mount_session_volume": False,  # Always ephemeral for factory workers
    }

    redis_client = redis.from_url(settings.redis_url)

    # Unique consumer ID for this request listener
    consumer_id = f"langgraph-{request_id[:8]}"
    group_name = f"langgraph-client-{request_id[:8]}"

    try:
        # 1. Publish Create Command
        create_payload = {
            "command": "create",
            "request_id": request_id,
            "config": config,
            "context": {"source": "langgraph"},
        }

        await redis_client.xgroup_create(RESPONSE_STREAM, group_name, id="$", mkstream=True)
        await redis_client.xadd(COMMAND_STREAM, {"data": json.dumps(create_payload)})
        logger.info("worker_spawn_requested", request_id=request_id, repo=repo)

        # 2. Wait for Creation Response
        create_resp = await _wait_for_response(
            redis_client, group_name, consumer_id, request_id, CREATION_TIMEOUT
        )

        if not create_resp:
            return SpawnResult(request_id, False, -1, "Timeout waiting for container creation")

        if not create_resp.get("success"):
            return SpawnResult(
                request_id, False, -1, f"Creation failed: {create_resp.get('error')}"
            )

        agent_id = create_resp.get("agent_id")

        # 3. Send Task Content as File (for AGENTS.md only, message goes direct now)
        if agents_content:
            agents_file_payload = {
                "command": "send_file",
                "request_id": f"{request_id}-agents",
                "agent_id": agent_id,
                "path": "/workspace/AGENTS.md",
                "content": agents_content,
            }
            await redis_client.xadd(COMMAND_STREAM, {"data": json.dumps(agents_file_payload)})

        # 4. Build task message with context
        task_message = f"""{task_title}

{task_content}

After completing the task:
1. Commit all changes with descriptive message
2. Push to the repository
"""

        # 5. Send message via headless protocol (no shell escaping needed)
        msg_payload = {
            "command": "send_message",
            "request_id": f"{request_id}-exec",
            "agent_id": agent_id,
            "message": task_message,
            "timeout": timeout_seconds,
        }

        await redis_client.xadd(COMMAND_STREAM, {"data": json.dumps(msg_payload)})

        # 6. Wait for Execution Response
        exec_result = await _wait_for_response(
            redis_client, group_name, consumer_id, f"{request_id}-exec", float(timeout_seconds)
        )

        # DELETE container
        await redis_client.xadd(
            COMMAND_STREAM,
            {
                "data": json.dumps(
                    {"command": "delete", "request_id": f"{request_id}-del", "agent_id": agent_id}
                )
            },
        )

        if exec_result:
            # send_message returns 'response' not 'output'
            return SpawnResult(
                request_id=request_id,
                success=exec_result.get("success", False),
                exit_code=0 if exec_result.get("success") else 1,
                output=exec_result.get("response", ""),
                error_message=exec_result.get("error"),
            )
        else:
            return SpawnResult(request_id, False, -1, "Timeout waiting for execution")

    except Exception as e:
        logger.error("spawn_failed", error=str(e))
        return SpawnResult(request_id, False, -1, str(e))
    finally:
        # Cleanup consumer group
        try:
            await redis_client.xgroup_destroy(RESPONSE_STREAM, group_name)
        except Exception as e:
            logger.warning("cleanup_failed", error=str(e))
        await redis_client.aclose()
