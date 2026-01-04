"""Redis command handlers for workers-spawner."""

from typing import Any

import redis.asyncio as redis
import structlog

from workers_spawner.config import get_settings
from workers_spawner.container_service import ContainerService
from workers_spawner.events import EventPublisher
from workers_spawner.log_collector import LogCollector
from workers_spawner.models import WorkerConfig
from workers_spawner.process_manager import ProcessManager

logger = structlog.get_logger()


class CommandHandler:
    """Handles Redis commands for agent management."""

    def __init__(
        self,
        redis_client: redis.Redis,
        container_service: ContainerService,
        event_publisher: EventPublisher,
        process_manager: ProcessManager | None = None,
        log_collector: LogCollector | None = None,
    ):
        self.redis = redis_client
        self.containers = container_service
        self.events = event_publisher
        self.settings = get_settings()
        self.process_manager = process_manager
        self.log_collector = log_collector

        # Command handlers map
        self._handlers = {
            "create": self._handle_create,
            "send_command": self._handle_send_command,
            "send_message": self._handle_send_message,
            "send_message_persistent": self._handle_send_message_persistent,
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
        - persistent: optional bool (default: False) - use persistent mode
        """
        config_data = message.get("config")
        if not config_data:
            raise ValueError("Missing 'config' field")

        config = WorkerConfig(**config_data)
        context = message.get("context", {})
        use_persistent = message.get("persistent", False)

        agent_id = await self.containers.create_container(config, context)

        # Start persistent process if requested and ProcessManager available
        if use_persistent and self.process_manager:
            from workers_spawner.factories.registry import get_agent_factory

            factory = get_agent_factory(config.agent, self.containers)

            # Store factory in metadata for later use
            if agent_id in self.containers._containers:
                self.containers._containers[agent_id]["factory"] = factory

            await self.process_manager.start_process(agent_id, factory)

            # Start log collector if available
            if self.log_collector:
                await self.log_collector.start_collecting(agent_id, self.process_manager)

            logger.info("persistent_process_started", agent_id=agent_id)

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

    async def _handle_send_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle send_message command - high-level API for agent communication.

        Expected fields:
        - agent_id: str
        - message: str (user message text)

        Returns:
        - response: str (agent response text)
        - metadata: dict (optional agent metadata)
        """
        agent_id = message.get("agent_id")
        user_message = message.get("message")

        if not agent_id or not user_message:
            raise ValueError("Missing 'agent_id' or 'message' field")

        # Get container metadata to determine agent type
        metadata = self.containers._containers.get(agent_id)
        if not metadata:
            raise ValueError(f"Agent {agent_id} not found")

        config: WorkerConfig = metadata["config"]

        # Get factory for this agent type
        from workers_spawner.factories.registry import get_agent_factory

        factory = get_agent_factory(config.agent, self.containers)

        # Get session context from Redis
        session_context = await self.containers.session_manager.get_session_context(agent_id)

        # Send message through factory
        result = await factory.send_message(
            agent_id=agent_id,
            message=user_message,
            session_context=session_context,
        )

        # Save updated session context
        new_context = result.get("session_context")
        if new_context:
            ttl = config.ttl_hours * 3600
            await self.containers.session_manager.save_session_context(
                agent_id, new_context, ttl_seconds=ttl
            )

        # Publish message event for logging/analytics
        await self.events.publish_message(
            agent_id=agent_id,
            role="assistant",
            content=result["response"],
        )

        return {
            "response": result["response"],
            "metadata": result.get("metadata", {}),
        }

    async def _handle_send_message_persistent(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle send_message for persistent agents.

        Writes message to agent's stdin. Agent responds via `orchestrator respond` CLI
        which writes directly to Redis. This method returns immediately.

        Expected fields:
        - agent_id: str
        - message: str (user message text)

        Returns:
        - sent: bool (True if message was written to stdin)
        """
        agent_id = message.get("agent_id")
        user_message = message.get("message")

        if not agent_id or not user_message:
            raise ValueError("Missing 'agent_id' or 'message' field")

        if not self.process_manager:
            raise RuntimeError("ProcessManager not configured")

        if not self.process_manager.is_running(agent_id):
            raise RuntimeError(f"No persistent process for agent {agent_id}")

        # Write message to stdin
        await self.process_manager.write_to_stdin(agent_id, user_message)

        logger.info(
            "message_sent_to_persistent_agent",
            agent_id=agent_id,
            message_length=len(user_message),
        )

        # Agent will respond via `orchestrator respond` CLI → Redis
        # Don't wait for response here
        return {"sent": True}

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

        # Stop persistent process if running
        if self.process_manager and self.process_manager.is_running(agent_id):
            await self.process_manager.stop_process(agent_id)
            logger.info("persistent_process_stopped", agent_id=agent_id)

        # Stop log collector
        if self.log_collector:
            await self.log_collector.stop_collecting(agent_id)

        success = await self.containers.delete(agent_id)

        if success:
            await self.events.publish_status(agent_id, "deleted")

        return {"deleted": success}
