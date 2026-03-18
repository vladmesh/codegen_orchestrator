import asyncio

import structlog
from pydantic import TypeAdapter, ValidationError

from shared.contracts.queues.worker import (
    WorkerCommand,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    StatusWorkerCommand,
    CreateWorkerResponse,
    DeleteWorkerResponse,
    StatusWorkerResponse,
    WorkerResponse,
)
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.queues import WORKER_COMMANDS, WORKER_MANAGER_GROUP, WORKER_RESPONSES
from shared.redis_client import RedisStreamClient

from .manager import WorkerManager

logger = structlog.get_logger()


class WorkerCommandConsumer:
    def __init__(self, client: RedisStreamClient, manager: WorkerManager):
        self.client = client
        self.manager = manager
        self.stream_name = WORKER_COMMANDS
        self.group_name = WORKER_MANAGER_GROUP
        self.consumer_name = "worker_manager_1"  # In prod, use hostname/podname

    async def run(self):
        """Run consumer loop."""
        logger.info("worker_consumer_started")

        async for msg in self.client.consume(
            self.stream_name,
            self.group_name,
            self.consumer_name,
            count=10,
            auto_ack=False,
            claim_pending=True,
        ):
            if msg is None:
                continue
            try:
                bind_message_context(msg.data)
                await self.process_message(msg.message_id, msg.data)
                await self.client.ack(self.stream_name, self.group_name, msg.message_id)
            except asyncio.CancelledError:
                logger.info("worker_consumer_stopping")
                break
            except Exception as e:
                logger.error(
                    "worker_consumer_message_error",
                    message_id=msg.message_id,
                    error=str(e),
                )
            finally:
                unbind_message_context()

    async def process_message(self, message_id: str, data: dict):
        """Process a single message."""
        logger.info("processing_message", message_id=message_id)

        try:
            adapter = TypeAdapter(WorkerCommand)
            command = adapter.validate_python(data)

            response = await self.handle_command(command)
            if response:
                await self.publish_response(command, response)

        except ValidationError as e:
            logger.error("invalid_command_format", error=str(e), message_id=message_id)
        except Exception as e:
            logger.error("command_processing_failed", error=str(e), message_id=message_id)

    async def handle_command(self, command: WorkerCommand) -> WorkerResponse | None:
        """Dispatch command to manager."""
        try:
            if isinstance(command, CreateWorkerCommand):
                return await self._handle_create(command)
            elif isinstance(command, DeleteWorkerCommand):
                return await self._handle_delete(command)
            elif isinstance(command, StatusWorkerCommand):
                return await self._handle_status(command)
            return None
        except Exception as e:
            logger.error("handler_error", error=str(e), command=command.command)
            # Return error response
            return self._create_error_response(command, str(e))

    async def _handle_create(self, cmd: CreateWorkerCommand) -> CreateWorkerResponse:
        """Handle create command with early ACK.

        Sends an immediate response with worker_id so the spawner can start
        polling worker status, then performs the heavy work (image build,
        container creation) which may take minutes on cache miss.
        """
        from .config import settings

        worker_id = cmd.config.name

        # Validate early (project lock, retry limit) — these are fast checks
        # done inside create_worker_with_capabilities before the heavy work.
        # Send early ACK with worker_id so spawner can poll status.
        early_resp = CreateWorkerResponse(request_id=cmd.request_id, success=True, worker_id=worker_id)
        await self.publish_response(cmd, early_resp)

        try:
            caps = [c.value for c in cmd.config.capabilities]

            env_vars = dict(cmd.config.env_vars)
            if user_id := cmd.context.get("user_telegram_id"):
                env_vars["ORCHESTRATOR_USER_ID"] = user_id

            await self.manager.create_worker_with_capabilities(
                worker_id=worker_id,
                capabilities=caps,
                base_image=settings.WORKER_BASE_IMAGE,
                agent_type=cmd.config.agent_type.value,
                instructions=cmd.config.instructions,
                task_content=cmd.config.task_content,
                env_vars=env_vars,
                auth_mode=cmd.config.auth_mode,
                host_claude_dir=cmd.config.host_claude_dir or settings.HOST_CLAUDE_DIR,
                api_key=cmd.config.api_key,
                worker_type=cmd.config.worker_type,
                project_id=cmd.config.project_id,
                repo_id=cmd.config.repo_id,
                scaffold_config=cmd.config.scaffold_config,
                branch=cmd.config.branch,
            )
            # No return — early ACK already sent, status is RUNNING in Redis
            return None
        except Exception as e:
            logger.error("worker_creation_failed_after_ack", worker_id=worker_id, error=str(e))
            # Worker status is already FAILED in Redis (set by manager cleanup)
            # No second response needed — spawner polls status and will see FAILED
            return None

    async def _handle_delete(self, cmd: DeleteWorkerCommand) -> DeleteWorkerResponse:
        try:
            await self.manager.delete_worker(cmd.worker_id, reason=cmd.reason)
            return DeleteWorkerResponse(request_id=cmd.request_id, success=True)
        except Exception as e:
            return DeleteWorkerResponse(request_id=cmd.request_id, success=False, error=str(e))

    async def _handle_status(self, cmd: StatusWorkerCommand) -> StatusWorkerResponse:
        try:
            status = await self.manager.get_worker_status(cmd.worker_id)
            return StatusWorkerResponse(
                request_id=cmd.request_id,
                success=True,
                status=status.lower(),  # map to literal
            )
        except Exception as e:
            return StatusWorkerResponse(request_id=cmd.request_id, success=False, error=str(e))

    def _create_error_response(self, cmd: WorkerCommand, error: str) -> WorkerResponse:
        if isinstance(cmd, CreateWorkerCommand):
            return CreateWorkerResponse(request_id=cmd.request_id, success=False, error=error)
        if isinstance(cmd, DeleteWorkerCommand):
            return DeleteWorkerResponse(request_id=cmd.request_id, success=False, error=error)
        if isinstance(cmd, StatusWorkerCommand):
            return StatusWorkerResponse(request_id=cmd.request_id, success=False, error=error)
        # Fallback?
        return None

    async def publish_response(self, cmd: WorkerCommand, response: WorkerResponse):
        """Publish response to developer response queue."""
        await self.client.publish(WORKER_RESPONSES, response.model_dump(mode="json"))
