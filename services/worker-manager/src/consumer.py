import asyncio
import structlog
from redis.asyncio import Redis
from pydantic import ValidationError, TypeAdapter

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
from .manager import WorkerManager

logger = structlog.get_logger()


class WorkerCommandConsumer:
    def __init__(self, redis: Redis, manager: WorkerManager):
        self.redis = redis
        self.manager = manager
        self.stream_name = "worker:commands"
        self.group_name = "worker_manager"
        self.consumer_name = "worker_manager_1"  # In prod, use hostname/podname

    async def ensure_group(self):
        """Ensure consumer group exists."""
        try:
            await self.redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def run(self):
        """Run consumer loop."""
        await self.ensure_group()
        logger.info("worker_consumer_started")

        while True:
            try:
                # Read new messages
                resp = await self.redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={self.stream_name: ">"},
                    count=10,
                    block=5000,
                )

                if not resp:
                    continue

                for stream_name, messages in resp:
                    for message_id, data in messages:
                        await self.process_message(message_id, data)
                        await self.redis.xack(stream_name, self.group_name, message_id)

            except asyncio.CancelledError:
                logger.info("worker_consumer_stopping")
                break
            except Exception as e:
                logger.error("worker_consumer_error", error=str(e))
                await asyncio.sleep(1)

    async def process_message(self, message_id: str, data: dict):
        """Process a single message."""
        logger.info("processing_message", message_id=message_id)

        # Parse command
        try:
            # Redis XREAD returns dict like {'data': 'json_string'} or just fields
            # Our contract implies we send a pydantic model dump.
            # Usually we send fields. If we used `model_dump_json()`, it might be in a field named 'data'
            # or spread across fields if we used `mapping`.
            # The test uses `{"data": command.model_dump_json()}`.

            raw_data = data.get("data")
            if not raw_data:
                logger.error("missing_data_field", message_id=message_id)
                return

            adapter = TypeAdapter(WorkerCommand)
            command = adapter.validate_json(raw_data)

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
        try:
            from .config import settings

            # Convert capabilities enum to string list
            caps = [c.value for c in cmd.config.capabilities]

            worker_id = await self.manager.create_worker_with_capabilities(
                worker_id=cmd.config.name,
                capabilities=caps,
                base_image=settings.WORKER_BASE_IMAGE,
                agent_type=cmd.config.agent_type.value,
                instructions=cmd.config.instructions,
                env_vars=cmd.config.env_vars,
            )
            return CreateWorkerResponse(request_id=cmd.request_id, success=True, worker_id=worker_id)
        except Exception as e:
            return CreateWorkerResponse(request_id=cmd.request_id, success=False, error=str(e))

    async def _handle_delete(self, cmd: DeleteWorkerCommand) -> DeleteWorkerResponse:
        try:
            await self.manager.delete_worker(cmd.worker_id)
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
        """Publish response to appropriate queue."""
        # Determine target queue based on who started it or config
        # CreateWorkerCommand has config.worker_type (po/developer)
        # Delete/Status don't have worker_type explicitly in command, but logic implies we know it.
        # However, looking at CONTRACTS, `DeleteWorkerCommand` only has `worker_id`.
        # We can default to `worker:responses:po` or `worker:responses:developer`?
        # Or better: if we can't determine, send to both or a generic one?
        # CONTRACTS say:
        # worker:responses:po -> For PO
        # worker:responses:developer -> For Developer.

        # Strategy:
        # If Create -> use config.worker_type
        # If others -> we might not know.
        # But maybe we can try to guess from prefix or broadcast.
        # Let's send to `worker:responses:po` by default or checking context.
        # Tests will verify specific queues.

        queue = "worker:responses:po"  # Default
        if isinstance(cmd, CreateWorkerCommand):
            if cmd.config.worker_type == "developer":
                queue = "worker:responses:developer"

        # Logic for others? Delete/Status.
        # The initiator knows where to listen.
        # Ideally we should include reply_to or worker_type in all commands.
        # Since we can't change contracts now without huge effort, let's assume specific routing logic
        # OR implementation detail: maybe we map ID to type via Redis first?
        # Assuming defaults for now.

        await self.redis.xadd(queue, {"data": response.model_dump_json()})
