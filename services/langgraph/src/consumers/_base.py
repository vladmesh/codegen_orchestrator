"""Base worker loop for Redis Stream queue consumers.

Provides common boilerplate shared by engineering_worker and deploy_worker:
signal handling, consumer group setup, message reading, ACKing, and shutdown.

Includes a staleness guard: before processing, checks if the referenced run/story
is already terminal (COMPLETED/FAILED/CANCELLED/ARCHIVED). If so, ACKs and skips.

Consumption is bounded by a slot gate. One slot is the historical behaviour —
read one entry, run it to its terminal outcome, then read the next — and it stays
the default. Above one, a consumer keeps reading while earlier jobs are still
running, so two projects can be worked on at the same time. The gate is resized
from runtime configuration without a redeploy, and a capacity of zero stops new
entries from being taken without touching the ones already in flight.

Running several jobs at once makes the PEL sweep dangerous in a way it is not
when processing is inline: XAUTOCLAIM only knows that an entry is idle, not that
its owner is alive, and an engineering turn is idle for its whole hour. Two
guards keep a reclaimed entry from becoming a second run of live work — an
in-process registry of the entries this consumer is running, and the durable
per-project live-work lease for every other process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
import os
import signal
import time

from pydantic import ValidationError
import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus
from shared.diagnostics import safe_validation_errors
from shared.log_config import setup_logging
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.queues import WORKER_GROUP
from shared.redis import RedisStreamClient

from ..clients.api import api_client
from ._live_work import execute_live_work, live_work_active

logger = structlog.get_logger(__name__)

# Type alias for job processor functions
ProcessFn = Callable[[dict, RedisStreamClient], Awaitable[dict]]
DrainCheck = Callable[[], Awaitable[bool | None]]

# One in-flight job per consumer: what every consumer did before slots existed.
DEFAULT_QUEUE_SLOTS = 1

# A ceiling on what runtime configuration may ask for. A coding worker is
# capped at 4 GiB and the orchestrator host is 8 GiB, so a mistyped slot count
# is an OOM that takes the API, Redis and the database with it. Raising the real
# limit is a measurement, not a config edit; this only stops a typo.
MAX_QUEUE_SLOTS = 4

# How often the slot count is re-read from configuration.
SLOT_REFRESH_SECONDS = 30

# How long a blocked reservation waits before re-checking shutdown and capacity.
SLOT_WAIT_SECONDS = 1.0

# How long shutdown waits for in-flight jobs before cancelling them. A cancelled
# job does not ACK, so its entry stays in the PEL and is reclaimed once its
# live-work lease expires.
SHUTDOWN_DRAIN_SECONDS = 10.0


async def _wait_while_draining(is_draining: DrainCheck | None, service_name: str) -> bool:
    """Yield a polling interval while the drain decision is active or unknown."""
    if is_draining is None:
        return False
    draining = await is_draining()
    if draining is False:
        return False
    logger.info(
        "consumer_drain_waiting",
        worker=service_name,
        decision="draining" if draining else "unknown",
    )
    await asyncio.sleep(SLOT_WAIT_SECONDS)
    return True


class TerminalMessageValidationError(Exception):
    """Validation failure raised only while parsing the Redis message."""

    def __init__(self, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__("invalid queue message")


# Module-level shutdown flag (set by signal handler)
_shutdown = False

# Terminal statuses — messages referencing these are stale
_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}
_TERMINAL_STORY_STATUSES = {
    StoryStatus.COMPLETED.value,
    StoryStatus.FAILED.value,
    StoryStatus.ARCHIVED.value,
}


def validate_queued_message(model, job_data: dict):
    """Parse a queue payload and mark only input validation as terminal."""
    try:
        return model.model_validate(job_data)
    except ValidationError as exc:
        raise TerminalMessageValidationError(exc) from exc


def _handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def _check_message_staleness(job_data: dict) -> bool:
    """Check if a queue message references a terminal run or story.

    Returns True if the message is stale and should be skipped.
    On API errors, returns False (proceed with processing).
    """
    task_id = job_data.get("task_id")
    if task_id:
        try:
            run_data = await api_client.get(f"runs/{task_id}")
            if run_data["status"] in _TERMINAL_RUN_STATUSES:
                logger.info(
                    "stale_message_skipped",
                    task_id=task_id,
                    run_status=run_data["status"],
                    reason="run_terminal",
                )
                return True
        except Exception:
            logger.debug("staleness_guard_api_error", task_id=task_id, exc_info=True)
        return False

    story_id = job_data.get("story_id")
    if story_id:
        try:
            story = await api_client.get_story(story_id)
            if story.status in _TERMINAL_STORY_STATUSES:
                logger.info(
                    "stale_message_skipped",
                    story_id=story_id,
                    story_status=story.status,
                    reason="story_terminal",
                )
                return True
        except Exception:
            logger.debug("staleness_guard_api_error", story_id=story_id, exc_info=True)
        return False

    return False


class SlotGate:
    """A resizable bound on how many jobs one consumer runs at once.

    Capacity is read from runtime configuration, so it changes under a running
    consumer. Shrinking never takes a slot away from a job that is already
    running: the gate simply stops handing out new ones until enough finish.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, capacity)
        self._in_flight = 0
        self._free = asyncio.Event()
        self._sync()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _sync(self) -> None:
        if self._in_flight < self._capacity:
            self._free.set()
        else:
            self._free.clear()

    def resize(self, capacity: int) -> bool:
        """Set a new capacity. Returns True when it actually changed."""
        capacity = max(0, capacity)
        if capacity == self._capacity:
            return False
        self._capacity = capacity
        self._sync()
        return True

    async def acquire(self, timeout: float) -> bool:
        """Take one slot, or report that none freed within `timeout`."""
        try:
            await asyncio.wait_for(self._free.wait(), timeout)
        except TimeoutError:
            return False
        if self._in_flight >= self._capacity:
            # Capacity shrank, or another waiter won the slot between the event
            # firing and this check.
            self._sync()
            return False
        self._in_flight += 1
        self._sync()
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self._sync()


def _clamp_slots(value: int, worker: str) -> int:
    if value > MAX_QUEUE_SLOTS:
        logger.warning(
            "queue_slots_clamped",
            worker=worker,
            requested=value,
            applied=MAX_QUEUE_SLOTS,
        )
        return MAX_QUEUE_SLOTS
    return max(0, value)


async def _read_configured_slots(config_key: str | None, current: int, worker: str) -> int:
    """Read the configured slot count, keeping the current one on any doubt.

    An unreadable or nonsensical configuration must not change how much work a
    running consumer takes: it keeps running on the number it already had.
    """
    if not config_key:
        return current
    try:
        config = await api_client.get(f"system-configs/{config_key}")
        value = config["value"] if isinstance(config, dict) else None
        slots = int(value)  # type: ignore[arg-type]
    except Exception:
        logger.warning(
            "queue_slots_config_unavailable", worker=worker, key=config_key, slots=current
        )
        return current
    if slots < 0:
        logger.warning(
            "queue_slots_config_invalid", worker=worker, key=config_key, value=slots, slots=current
        )
        return current
    return _clamp_slots(slots, worker)


async def _reclaimed_entry_is_live(redis: RedisStreamClient, msg, worker: str) -> bool:
    """Report whether a reclaimed entry still belongs to a working consumer.

    XAUTOCLAIM hands back an entry because it is idle, and an engineering turn
    is idle for its whole hour. The live-work lease is the only thing that knows
    the difference between an idle entry and a dead owner. An unreadable lease
    is not proof of death, so it counts as live: a delayed job is recoverable,
    a job running twice in one workspace is not.
    """
    project_id = msg.data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return False
    try:
        return await live_work_active(redis, project_id)
    except Exception:
        logger.warning(
            "live_work_lease_unreadable",
            worker=worker,
            entry_id=msg.message_id,
            project_id=project_id,
            exc_info=True,
        )
        return True


async def _process_entry(
    msg,
    redis: RedisStreamClient,
    queue: str,
    group: str,
    service_name: str,
    process_fn: ProcessFn,
) -> None:
    """Run one queue entry to its terminal outcome.

    Each entry runs in its own task, so the correlation bindings below are
    per-task: `asyncio.create_task` copies the contextvar context, and sibling
    jobs never see each other's task_id or project_id.
    """
    try:
        bind_message_context(msg.data)

        # Staleness guard: skip messages for terminal runs/stories
        if await _check_message_staleness(msg.data):
            await redis.ack(queue, group, msg.message_id)
            logger.debug("stale_job_acked", entry_id=msg.message_id, worker=service_name)
            return

        project_id = msg.data.get("project_id")
        result = await execute_live_work(
            redis,
            queue=queue,
            group=group,
            message_id=msg.message_id,
            project_id=project_id if isinstance(project_id, str) and project_id else None,
            process=lambda data=msg.data: process_fn(data, redis),
        )
        if result is not None:
            msg.data.update(result)
            logger.debug("job_acked", entry_id=msg.message_id, worker=service_name)
    except TerminalMessageValidationError as exc:
        # A schema error cannot become valid when reclaimed from the PEL.
        # ACK it after recording a payload-safe terminal diagnostic.
        logger.error(
            "terminal_message_validation_failed",
            entry_id=msg.message_id,
            worker=service_name,
            errors=safe_validation_errors(exc.validation_error),
        )
        try:
            await redis.ack(queue, group, msg.message_id)
        except Exception as ack_exc:
            logger.error(
                "terminal_message_ack_failed",
                entry_id=msg.message_id,
                worker=service_name,
                error_type=type(ack_exc).__name__,
                exc_info=True,
            )
    except asyncio.CancelledError:
        # Shutdown or teardown took this job. It was not ACKed, so the entry
        # stays in the PEL and is reclaimed once its lease expires.
        logger.info("job_cancelled", entry_id=msg.message_id, worker=service_name)
        raise
    except Exception as exc:
        logger.error(
            "job_processing_error",
            entry_id=msg.message_id,
            error_type=type(exc).__name__,
            worker=service_name,
            exc_info=True,
        )
    finally:
        unbind_message_context()


async def _drain_in_flight(in_flight: dict[str, asyncio.Task[None]], service_name: str) -> None:
    """Give in-flight jobs a bounded chance to settle, then cancel the rest."""
    if not in_flight:
        return
    logger.info("worker_draining", worker=service_name, jobs=len(in_flight))
    _done, pending = await asyncio.wait(list(in_flight.values()), timeout=SHUTDOWN_DRAIN_SECONDS)
    if not pending:
        return
    logger.warning("worker_drain_cancelling", worker=service_name, jobs=len(pending))
    for task in pending:
        task.cancel()
    await asyncio.wait(pending, timeout=SHUTDOWN_DRAIN_SECONDS)


async def run_queue_worker(
    service_name: str,
    queue: str,
    process_fn: ProcessFn,
    group: str = WORKER_GROUP,
    *,
    slots_config_key: str | None = None,
    default_slots: int = DEFAULT_QUEUE_SLOTS,
    is_draining: DrainCheck | None = None,
) -> None:
    """Generic worker loop for Redis Stream queue consumption.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
        group: Consumer group name (defaults to WORKER_GROUP)
        slots_config_key: System-config key holding how many jobs this consumer
            may run at once. Omitted means a fixed `default_slots`.
        default_slots: Slot count used before configuration is read, and when it
            cannot be read at all.
        is_draining: Optional durable operator decision that stops new claims
            while already-started jobs continue to settle.
    """
    global _shutdown
    _shutdown = False

    setup_logging(service_name=service_name)

    consumer_name = f"{service_name}-{os.getpid()}"

    redis = RedisStreamClient()
    await redis.connect()

    gate = SlotGate(
        await _read_configured_slots(
            slots_config_key, _clamp_slots(default_slots, service_name), service_name
        )
    )
    in_flight: dict[str, asyncio.Task[None]] = {}
    next_slot_refresh = time.monotonic() + SLOT_REFRESH_SECONDS

    logger.info(f"{service_name}_started", consumer=consumer_name, slots=gate.capacity)

    async def refresh_slots() -> None:
        nonlocal next_slot_refresh
        if slots_config_key is None or time.monotonic() < next_slot_refresh:
            return
        next_slot_refresh = time.monotonic() + SLOT_REFRESH_SECONDS
        resolved = await _read_configured_slots(slots_config_key, gate.capacity, service_name)
        if gate.resize(resolved):
            logger.info(
                "queue_slots_changed",
                worker=service_name,
                slots=gate.capacity,
                in_flight=gate.in_flight,
            )

    async def reserve_slot() -> bool:
        """Hold one slot for the next entry. False only when shutting down."""
        while not _shutdown:
            if await _wait_while_draining(is_draining, service_name):
                continue
            await refresh_slots()
            if await gate.acquire(SLOT_WAIT_SECONDS):
                return True
        return False

    def on_job_done(message_id: str, _task: asyncio.Task[None]) -> None:
        in_flight.pop(message_id, None)
        gate.release()

    try:
        if not await reserve_slot():
            return

        async for msg in redis.consume(
            queue,
            group,
            consumer_name,
            auto_ack=False,
            claim_pending=True,
        ):
            if _shutdown:
                break
            # XREADGROUP has already put a non-empty entry in this consumer's
            # PEL. Do not dispatch or ACK it while drained: a replacement
            # consumer reclaims it through the ordinary handoff path.
            if await _wait_while_draining(is_draining, service_name):
                continue
            if msg is None:
                await refresh_slots()
                continue
            # The sweep runs while this consumer's own jobs are in flight, and
            # they are idle for as long as they run. Their entries come back
            # here; taking one would be the same job twice in one workspace.
            if msg.message_id in in_flight:
                logger.debug(
                    "inflight_entry_reclaim_skipped",
                    entry_id=msg.message_id,
                    worker=service_name,
                )
                continue

            if msg.reclaimed and await _reclaimed_entry_is_live(redis, msg, service_name):
                logger.info(
                    "live_entry_reclaim_skipped",
                    entry_id=msg.message_id,
                    worker=service_name,
                    project_id=msg.data.get("project_id"),
                )
                continue

            # The reserved slot becomes this job's; the callback returns it.
            task = asyncio.create_task(
                _process_entry(msg, redis, queue, group, service_name, process_fn)
            )
            in_flight[msg.message_id] = task
            task.add_done_callback(partial(on_job_done, msg.message_id))

            if not await reserve_slot():
                break
    finally:
        await _drain_in_flight(in_flight, service_name)
        await redis.close()
        await api_client.close()
        logger.info(f"{service_name}_shutdown")


def start_worker(
    service_name: str,
    queue: str,
    process_fn: ProcessFn,
    group: str = WORKER_GROUP,
    *,
    slots_config_key: str | None = None,
    default_slots: int = DEFAULT_QUEUE_SLOTS,
    is_draining: DrainCheck | None = None,
) -> None:
    """Entry point: register signal handlers and run the worker loop.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
        group: Consumer group name (defaults to WORKER_GROUP)
        slots_config_key: System-config key holding this consumer's slot count
        default_slots: Slot count used until configuration answers
        is_draining: Optional durable operator decision that stops new claims.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    asyncio.run(
        run_queue_worker(
            service_name,
            queue,
            process_fn,
            group=group,
            slots_config_key=slots_config_key,
            default_slots=default_slots,
            is_draining=is_draining,
        )
    )
