"""Lifecycle manager for container TTL and cleanup."""

import asyncio
from datetime import UTC, datetime

import structlog

from workers_spawner.config import get_settings
from workers_spawner.container_service import ContainerService
from workers_spawner.events import EventPublisher

logger = structlog.get_logger()

# Constants
IDLE_PAUSE_MINUTES = 30  # Auto-pause containers idle for this long


class LifecycleManager:
    """Manages container lifecycle: TTL, auto-pause, cleanup."""

    def __init__(
        self,
        container_service: ContainerService,
        event_publisher: EventPublisher,
    ):
        self.containers = container_service
        self.events = event_publisher
        self.settings = get_settings()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the lifecycle management background task."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("lifecycle_manager_started")

    async def stop(self) -> None:
        """Stop the lifecycle management task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("lifecycle_manager_stopped")

    async def _run_loop(self) -> None:
        """Main lifecycle check loop."""
        check_interval = 60  # Check every minute

        while self._running:
            try:
                await self._check_containers()
            except Exception as e:
                logger.error("lifecycle_check_error", error=str(e))

            await asyncio.sleep(check_interval)

    async def _check_containers(self) -> None:
        """Check all containers for TTL expiration and idle state."""
        now = datetime.now(UTC)

        for agent_id, metadata in list(self.containers._containers.items()):
            try:
                # Check TTL
                created_at = datetime.fromisoformat(metadata["created_at"])
                ttl_hours = metadata.get("ttl_hours", self.settings.default_ttl_hours)
                age_hours = (now - created_at).total_seconds() / 3600

                if age_hours > ttl_hours:
                    logger.info(
                        "ttl_expired",
                        agent_id=agent_id,
                        age_hours=age_hours,
                        ttl_hours=ttl_hours,
                    )
                    await self.containers.delete(agent_id)
                    await self.events.publish_status(agent_id, "expired")
                    continue

                # Check for idle containers (no activity for 30 min)
                last_activity = metadata.get("last_activity")
                if last_activity:
                    last_activity_dt = datetime.fromisoformat(last_activity)
                    idle_minutes = (now - last_activity_dt).total_seconds() / 60

                    if idle_minutes > IDLE_PAUSE_MINUTES and metadata.get("state") == "running":
                        logger.info(
                            "auto_pausing_idle",
                            agent_id=agent_id,
                            idle_minutes=idle_minutes,
                        )
                        await self.containers.pause(agent_id)
                        await self.events.publish_status(agent_id, "paused")

            except Exception as e:
                logger.error(
                    "container_check_error",
                    agent_id=agent_id,
                    error=str(e),
                )
