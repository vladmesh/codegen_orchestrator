import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from redis.asyncio import Redis
import structlog

from shared.redis_client import RedisStreamClient

from .config import settings
from .manager import WorkerManager
from .consumer import WorkerCommandConsumer
from .events import DockerEventsListener
from .compose_runner import ComposeRunner
from .routers.compose import router as compose_router

logger = structlog.get_logger()

# Global instances
redis: Redis | None = None
worker_manager: WorkerManager | None = None
events_listener: DockerEventsListener | None = None


async def run_periodic_task(coro_func, interval: int, name: str):
    """Run a periodic task in an infinite loop."""
    logger.info("periodic_task_started", task=name, interval=interval)
    while True:
        try:
            await coro_func()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("periodic_task_error", task=name, error=str(e))

        await asyncio.sleep(interval)
    logger.info("periodic_task_stopped", task=name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global redis, worker_manager
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    worker_manager = WorkerManager(redis)

    # Shared state for HTTP handlers
    app.state.compose_runner = ComposeRunner(settings.WORKSPACE_BASE_PATH)
    app.state.docker = worker_manager.docker
    app.state.redis = redis

    # Start Consumer
    stream_client = RedisStreamClient(redis_url=settings.REDIS_URL)
    await stream_client.connect()
    consumer = WorkerCommandConsumer(stream_client, worker_manager)
    consumer_task = asyncio.create_task(consumer.run())

    # Start Docker Events Listener
    global events_listener
    events_listener = DockerEventsListener(redis)
    events_task = asyncio.create_task(events_listener.start())

    # Start Periodic Tasks
    # GC every hour (3600s)
    gc_task = asyncio.create_task(
        run_periodic_task(lambda: worker_manager.garbage_collect_images(), interval=3600, name="garbage_collect")
    )

    # Auto-Pause every minute (60s)
    pause_task = asyncio.create_task(
        run_periodic_task(lambda: worker_manager.check_and_pause_workers(), interval=60, name="auto_pause")
    )

    # Orphaned resource GC every 30 minutes (1800s)
    orphan_gc_task = asyncio.create_task(
        run_periodic_task(
            lambda: worker_manager.garbage_collect_orphaned_resources(),
            interval=1800,
            name="orphaned_gc",
        )
    )

    # Workspace GC every 6 hours (21600s)
    workspace_gc_task = asyncio.create_task(
        run_periodic_task(
            lambda: worker_manager.garbage_collect_workspaces(max_age_hours=24),
            interval=21600,
            name="workspace_gc",
        )
    )

    yield

    # Shutdown
    logger.info("shutdown_initiated")

    consumer_task.cancel()
    events_task.cancel()
    if events_listener:
        events_listener.stop()

    gc_task.cancel()
    pause_task.cancel()
    orphan_gc_task.cancel()
    workspace_gc_task.cancel()

    try:
        await asyncio.gather(
            consumer_task,
            events_task,
            gc_task,
            pause_task,
            orphan_gc_task,
            workspace_gc_task,
            return_exceptions=True,
        )
    except Exception:
        pass

    await redis.close()
    logger.info("shutdown_complete")


app = FastAPI(title="Worker Manager", lifespan=lifespan)
app.include_router(compose_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "environment": settings.ENVIRONMENT}
