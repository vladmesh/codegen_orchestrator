"""Workers Spawner Client for Telegram Bot.

Handles interaction with the workers-spawner service via Redis Streams.
Implements an async request-response pattern using a background listener.
"""

import asyncio
from typing import Any
import uuid

import structlog

from shared.redis_client import RedisStreamClient
from src.config import get_settings

logger = structlog.get_logger(__name__)

COMMAND_STREAM = "cli-agent:commands"
RESPONSE_STREAM = "cli-agent:responses"
CONSUMER_GROUP = "telegram-bot-client"


class WorkersSpawnerClient:
    """Client for workers-spawner service."""

    def __init__(self) -> None:
        settings = get_settings()
        self.redis = RedisStreamClient(settings.redis_url)
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._listener_task: asyncio.Task | None = None
        self._consumer_id = f"bot-{uuid.uuid4().hex[:8]}"

    async def connect(self) -> None:
        """Connect to Redis and start response listener."""
        await self.redis.connect()
        self._listener_task = asyncio.create_task(self._listen_for_responses())
        logger.info("workers_spawner_client_connected")

    async def close(self) -> None:
        """Close connection and stop listener."""
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        await self.redis.close()

    async def _listen_for_responses(self) -> None:
        """Background task to listen for responses."""
        try:
            # We use a fan-out pattern for responses if possible,
            # but streams are consumer-group based.
            # If we used a shared group with other bots, we might steal messages.
            # But here we assume one bot instance or we need a unique group per instance?
            # Responses in workers-spawner are published to a stream.
            # If multiple clients are listening, they need to see ALL messages or their own.
            # Redis Streams: messages in a consumer group are distributed.
            # So if we have multiple bot replicas, using one group means only one gets the response.
            # If that replica is NOT the one who sent the request, the request hangs.
            # SOLUTION: Use unique consumer group per instance (or fan-out).
            # Or use XREAD (without groups) to see all messages?
            # XREAD from $ (now) is what we want for "broadcast" style if we filter by ID.
            # But standard XREAD is for new messages.

            # For simplicity in this architecture (single bot instance usually),
            # we use a unique group name per instance to ensure we get a copy of messages
            # if we wanted persistent queues, but XREAD is better for "RPC response"
            # where we don't care about history if we crash (request fails anyway).

            # Checking shared/redis_client.py, it has `consume` which uses groups.
            # We'll use a unique group per instance to be safe.
            unique_group = f"{CONSUMER_GROUP}-{self._consumer_id}"

            logger.info("starting_response_listener", group=unique_group)

            async for message in self.redis.consume(
                stream=RESPONSE_STREAM,
                group=unique_group,
                consumer=self._consumer_id,
            ):
                data = message.data
                request_id = data.get("request_id")

                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests.pop(request_id)
                    if not future.done():
                        future.set_result(data)

                # Cleanup old pending requests? (futures handle checking logic)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("response_listener_error", error=str(e))
            # Restart listener? In production yes, for now recursive call or loop
            await asyncio.sleep(1)

    async def _request(self, command: str, payload: dict, timeout: float = 30.0) -> dict[str, Any]:
        """Send request and wait for response."""
        request_id = str(uuid.uuid4())
        payload["command"] = command
        payload["request_id"] = request_id

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            await self.redis.publish(COMMAND_STREAM, payload)

            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as e:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Request {command}:{request_id} timed out") from e
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise e

    async def create_agent(
        self,
        user_id: str,
        mount_session_volume: bool = False,
    ) -> str:
        """Create a new agent container for user.

        Args:
            user_id: Telegram user ID
            mount_session_volume: Whether to mount host session (dev mode)

        Returns:
            agent_id: Created agent ID
        """
        config = {
            "name": f"User {user_id}",
            "agent": "claude-code",
            "capabilities": ["git", "curl", "python", "node"],  # Default capabilities
            "allowed_tools": ["project", "deploy", "engineering", "infra", "respond", "diagnose"],
            "mount_session_volume": mount_session_volume,
        }

        context = {
            "user_id": str(user_id),
            "source": "telegram",
        }

        response = await self._request("create", {"config": config, "context": context})

        if not response.get("success", False):
            raise RuntimeError(f"Failed to create agent: {response.get('error')}")

        return response["agent_id"]

    async def send_command(
        self,
        agent_id: str,
        command: str,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Send command to agent.

        Returns:
            Dict with output, exit_code, error
        """
        response = await self._request(
            "send_command",
            {"agent_id": agent_id, "shell_command": command, "timeout": timeout},
            timeout=float(timeout + 5),
        )

        if not response.get("success", False):
            raise RuntimeError(f"Command failed: {response.get('error')}")

        return response

    async def get_status(self, agent_id: str) -> dict[str, Any] | None:
        """Get agent status.

        Returns:
            Status dict or None if not found
        """
        response = await self._request("status", {"agent_id": agent_id})

        if not response.get("found", False):
            return None

        return response.get("status")


# Singleton instance
workers_spawner = WorkersSpawnerClient()
