import structlog
from typing import Any, Dict
from shared.redis.client import RedisStreamClient

logger = structlog.get_logger()


class DockerEventsListener:
    """
    Listens for Docker events and handles worker crashes.
    """

    def __init__(self, redis_client: RedisStreamClient):
        self.redis = redis_client
        self._running = False

    async def start(self):
        """Start listening for Docker events."""
        self._running = True
        logger.info("docker_events_listener_started")

        # TODO: Implement actual event listening via DockerClient
        # For now, just sleep to keep the task alive
        import asyncio

        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        """Stop listening."""
        self._running = False
        logger.info("docker_events_listener_stopped")

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """
        Process a single Docker event.
        We are interested in 'die' events from worker containers with non-zero exit code.
        """
        # Filter for container die events
        if event.get("Type") != "container" or event.get("Action") != "die":
            return

        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})

        # Check if it's a worker container (by label)
        # We assume workers are launched with labels:
        # label_task_id, label_worker_type
        # If labels are missing, check name pattern as fallback? Code says ignore if not match.

        task_id = attributes.get("label_task_id")
        worker_type = attributes.get("label_worker_type")

        if not task_id or not worker_type:
            # Not a managed worker or missing info
            return

        exit_code = attributes.get("exitCode")

        if exit_code == "0":
            # Normal exit, ignore
            return

        logger.warning(
            "worker_crashed",
            task_id=task_id,
            worker_type=worker_type,
            exit_code=exit_code,
            container=attributes.get("name"),
        )

        # Publish failure to output queue
        stream_key = f"worker:{worker_type}:output"
        message = {
            "task_id": task_id,
            "type": "error",
            "content": f"Worker container crashed with exit code {exit_code}",
            "metadata": {"exit_code": exit_code, "container_id": actor.get("ID")},
        }

        await self.redis.xadd(stream_key, message)
