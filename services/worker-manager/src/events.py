"""Docker events listener for detecting worker container deaths.

Listens for Docker 'die' events on worker containers and publishes error
messages to the worker's output stream, unblocking any waiting consumers
(e.g., engineering-worker's _wait_for_response).
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import docker
import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

# Status value set when a container dies unexpectedly
WORKER_DEAD_STATUS = "DEAD"


class DockerEventsListener:
    """Listens for Docker events and handles worker container deaths."""

    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self._running = False
        self._events_stream = None

    async def start(self):
        """Listen for Docker container die events in a background thread."""
        self._running = True
        logger.info("docker_events_listener_started")

        client = docker.from_env()
        self._events_stream = client.events(
            decode=True,
            filters={
                "type": "container",
                "event": "die",
                "label": "com.codegen.type=worker",
            },
        )

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        queue: asyncio.Queue = asyncio.Queue()

        def _pump_events():
            """Read blocking Docker events stream in a thread, push to async queue."""
            try:
                for event in self._events_stream:
                    if not self._running:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as e:
                if self._running:
                    loop.call_soon_threadsafe(queue.put_nowait, {"_error": str(e)})

        pump_future = loop.run_in_executor(executor, _pump_events)

        try:
            while self._running:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue

                if "_error" in event:
                    logger.error("docker_events_stream_error", error=event["_error"])
                    break

                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.error("docker_event_handler_error", error=str(e))
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._events_stream:
                try:
                    self._events_stream.close()
                except Exception as e:
                    logger.debug("cleanup_events_stream_close_error", error=str(e))
            try:
                client.close()
            except Exception as e:
                logger.debug("cleanup_docker_client_close_error", error=str(e))
            try:
                await pump_future
            except Exception as e:
                logger.debug("cleanup_pump_future_error", error=str(e))
            executor.shutdown(wait=False)
            logger.info("docker_events_listener_stopped")

    def stop(self):
        """Stop listening."""
        self._running = False
        if self._events_stream:
            try:
                self._events_stream.close()
            except Exception as e:
                logger.debug("cleanup_events_stream_close_error", error=str(e))

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a Docker container die event for a worker container.

        Extracts worker_id from container labels, publishes an error message
        to the worker's output stream (unblocking _wait_for_response), and
        marks the worker status as DEAD in Redis.
        """
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})

        worker_id = attributes.get("com.codegen.worker.id")
        if not worker_id:
            return

        exit_code = attributes.get("exitCode", "unknown")
        container_name = attributes.get("name", "unknown")

        # Normal exit (0) means the worker finished — wrapper already published output
        if str(exit_code) == "0":
            logger.info("worker_exited_normally", worker_id=worker_id, container=container_name)
            return

        logger.warning(
            "worker_container_died",
            worker_id=worker_id,
            exit_code=exit_code,
            container=container_name,
        )

        # 1. Publish error to worker output stream (unblocks _wait_for_response)
        output_stream = f"worker:{worker_id}:output"
        error_payload = json.dumps(
            {
                "status": "failed",
                "error": f"Worker container died (exit_code={exit_code})",
                "worker_id": worker_id,
            }
        )
        try:
            await self.redis.xadd(output_stream, {"data": error_payload})
            logger.info("worker_death_published", worker_id=worker_id, stream=output_stream)
        except Exception as e:
            logger.error("worker_death_publish_failed", worker_id=worker_id, error=str(e))

        # 2. Mark worker status as DEAD so liveness checks also detect it
        try:
            await self.redis.hset(f"worker:status:{worker_id}", mapping={"status": WORKER_DEAD_STATUS})
        except Exception as e:
            logger.error("worker_status_update_failed", worker_id=worker_id, error=str(e))
