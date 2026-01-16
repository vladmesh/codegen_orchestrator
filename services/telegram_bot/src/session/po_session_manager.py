"""PO Session Manager - Redis Streams based.

Manages mapping between Telegram users and their PO Worker containers.
Communicates with worker-manager via Redis Streams instead of HTTP.
"""

import json
from typing import Any
import uuid

from redis.asyncio import Redis
import structlog

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerConfig,
)

logger = structlog.get_logger(__name__)

# Redis key prefixes
SESSION_KEY_PREFIX = "session:po:"
WORKER_STATUS_KEY_PREFIX = "worker:status:"

# Redis stream names
WORKER_COMMANDS_STREAM = "worker:commands"
WORKER_RESPONSES_STREAM = "worker:responses"


class POSessionManager:
    """Manages PO Worker sessions for Telegram users.

    Uses Redis Streams to communicate with worker-manager:
    - Publishes CreateWorkerCommand to `worker:commands`
    - Publishes user messages to `worker:po:{id}:input`
    - Listens for worker output on `worker:po:{id}:output`
    """

    def __init__(self, redis: Redis) -> None:
        """Initialize session manager.

        Args:
            redis: Async Redis client instance
        """
        self.redis = redis

    async def get_or_create_worker(self, user_id: int) -> str:
        """Get existing PO worker or create a new one.

        Args:
            user_id: Telegram user ID

        Returns:
            worker_id: Active PO worker ID
        """
        session_key = f"{SESSION_KEY_PREFIX}{user_id}"

        # 1. Check for existing session
        existing_worker_id = await self.redis.get(session_key)

        if existing_worker_id:
            # 2. Verify worker is still running
            status = await self._get_worker_status(existing_worker_id)
            if status and status.get("status") == "RUNNING":
                logger.info(
                    "reusing_existing_worker",
                    user_id=user_id,
                    worker_id=existing_worker_id,
                )
                return existing_worker_id

            logger.info(
                "existing_worker_not_running",
                user_id=user_id,
                worker_id=existing_worker_id,
                status=status,
            )

        # 3. Create new worker
        worker_id = await self._create_worker(user_id)

        # 4. Store session
        await self.redis.set(session_key, worker_id)

        logger.info(
            "new_worker_created",
            user_id=user_id,
            worker_id=worker_id,
        )

        return worker_id

    async def send_message(self, user_id: int, content: str) -> None:
        """Send user message to their PO worker.

        Args:
            user_id: Telegram user ID
            content: Message text
        """
        session_key = f"{SESSION_KEY_PREFIX}{user_id}"
        worker_id = await self.redis.get(session_key)

        if not worker_id:
            raise ValueError(f"No session found for user {user_id}")

        # Publish to worker input stream
        stream_key = f"worker:po:{worker_id}:input"
        payload = {
            "user_id": user_id,
            "content": content,
        }

        await self.redis.xadd(
            stream_key,
            {"data": json.dumps(payload)},
        )

        logger.info(
            "message_sent_to_worker",
            user_id=user_id,
            worker_id=worker_id,
            content_length=len(content),
        )

    async def _get_worker_status(self, worker_id: str) -> dict[str, Any] | None:
        """Get worker status from Redis hash.

        Args:
            worker_id: Worker container ID

        Returns:
            Status dict or None if not found
        """
        status_key = f"{WORKER_STATUS_KEY_PREFIX}{worker_id}"
        status = await self.redis.hgetall(status_key)
        return status if status else None

    async def _create_worker(self, user_id: int) -> str:
        """Create new PO worker via Redis command stream.

        Args:
            user_id: Telegram user ID

        Returns:
            worker_id: Created worker ID
        """
        request_id = str(uuid.uuid4())
        worker_name = f"po-{user_id}"

        # Build command
        command = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="po",
                agent_type=AgentType.CLAUDE,
                instructions="You are a Product Owner assistant.",
                allowed_commands=["*"],
                capabilities=[],
            ),
            context={
                "user_telegram_id": str(user_id),
            },
        )

        # Publish to worker commands stream
        await self.redis.xadd(
            WORKER_COMMANDS_STREAM,
            {"data": command.model_dump_json()},
        )

        logger.info(
            "create_worker_command_published",
            request_id=request_id,
            user_id=user_id,
        )

        # Wait for response (this method can be mocked in tests)
        response = await self._wait_for_worker_response(request_id, timeout=30.0)

        if not response or not response.get("success"):
            raise RuntimeError(f"Failed to create worker: {response}")

        return response["worker_id"]

    async def _wait_for_worker_response(
        self, request_id: str, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        """Wait for worker-manager response.

        This is a placeholder that should be overridden in tests.
        In production, this would listen to a response stream.

        Args:
            request_id: Request ID to match
            timeout: Max wait time in seconds

        Returns:
            Response dict or None on timeout
        """
        # TODO: Implement actual response listening
        # For now, this will be mocked in tests
        raise NotImplementedError(
            "_wait_for_worker_response must be mocked in tests "
            "or implemented with actual response stream listening"
        )
