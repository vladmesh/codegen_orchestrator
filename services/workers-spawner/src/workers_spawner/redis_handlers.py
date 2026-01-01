"""Redis command handlers for workers-spawner."""

from typing import Any

import redis.asyncio as redis
import structlog

from workers_spawner.config import get_settings
from workers_spawner.container_service import ContainerService
from workers_spawner.events import EventPublisher
from workers_spawner.models import WorkerConfig

logger = structlog.get_logger()


class CommandHandler:
    """Handles Redis commands for agent management."""

    def __init__(
        self,
        redis_client: redis.Redis,
        container_service: ContainerService,
        event_publisher: EventPublisher,
    ):
        self.redis = redis_client
        self.containers = container_service
        self.events = event_publisher
        self.settings = get_settings()

        # Command handlers map
        self._handlers = {
            "create": self._handle_create,
            "send_command": self._handle_send_command,
            "send_file": self._handle_send_file,
            "status": self._handle_status,
            "logs": self._handle_logs,
            "delete": self._handle_delete,
        }

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Route message to appropriate handler.

        Expected message format:
        {
            "command": "create|send_command|send_file|status|logs|delete",
            "request_id": "unique-id",
            ...command-specific fields
        }
        """
        command = message.get("command")
        request_id = message.get("request_id", "unknown")

        logger.info(
            "handling_command",
            command=command,
            request_id=request_id,
        )

        handler = self._handlers.get(command)
        if not handler:
            logger.warning("unknown_command", command=command)
            return {"success": False, "error": f"Unknown command: {command}"}

        try:
            result = await handler(message)
            return {"success": True, "request_id": request_id, **result}
        except Exception as e:
            logger.error(
                "command_failed",
                command=command,
                request_id=request_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return {"success": False, "request_id": request_id, "error": str(e)}

    async def _handle_create(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.create command.

        Expected fields:
        - config: WorkerConfig dict
        - context: optional dict with user_id, project_id, etc.
        """
        config_data = message.get("config")
        if not config_data:
            raise ValueError("Missing 'config' field")

        config = WorkerConfig(**config_data)
        context = message.get("context", {})

        agent_id = await self.containers.create_container(config, context)

        # Publish status change
        await self.events.publish_status(agent_id, "created")

        return {"agent_id": agent_id}

    async def _handle_send_command(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.send_command.

        Expected fields:
        - agent_id: str
        - shell_command: str
        - timeout: optional int
        """
        agent_id = message.get("agent_id")
        shell_command = message.get("shell_command")
        timeout = message.get("timeout")

        if not agent_id or not shell_command:
            raise ValueError("Missing 'agent_id' or 'shell_command' field")

        result = await self.containers.send_command(agent_id, shell_command, timeout)

        # Publish command exit event
        await self.events.publish_command_exit(agent_id, result.exit_code, result.output)

        return {
            "output": result.output,
            "exit_code": result.exit_code,
            "error": result.error,
        }

    async def _handle_send_file(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.send_file.

        Expected fields:
        - agent_id: str
        - path: str
        - content: str
        """
        agent_id = message.get("agent_id")
        path = message.get("path")
        content = message.get("content")

        if not agent_id or not path or content is None:
            raise ValueError("Missing required fields")

        success = await self.containers.send_file(agent_id, path, content)

        return {"success": success}

    async def _handle_status(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.status.

        Expected fields:
        - agent_id: str
        """
        agent_id = message.get("agent_id")
        if not agent_id:
            raise ValueError("Missing 'agent_id' field")

        status = await self.containers.get_status(agent_id)

        if status is None:
            return {"found": False}

        return {"found": True, "status": status.model_dump()}

    async def _handle_logs(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.logs.

        Expected fields:
        - agent_id: str
        - tail: optional int (default 100)
        """
        agent_id = message.get("agent_id")
        tail = message.get("tail", 100)

        if not agent_id:
            raise ValueError("Missing 'agent_id' field")

        logs = await self.containers.get_logs(agent_id, tail)

        return {"logs": logs}

    async def _handle_delete(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle cli-agent.delete.

        Expected fields:
        - agent_id: str
        """
        agent_id = message.get("agent_id")
        if not agent_id:
            raise ValueError("Missing 'agent_id' field")

        success = await self.containers.delete(agent_id)

        if success:
            await self.events.publish_status(agent_id, "deleted")

        return {"deleted": success}
