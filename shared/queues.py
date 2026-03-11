"""Redis Streams job queues for async task processing.

Provides queue constants and utilities for Phase 4 capability workers.
Single source of truth for all stream/group bindings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Stream names
# ---------------------------------------------------------------------------
DEPLOY_QUEUE = "deploy:queue"
ENGINEERING_QUEUE = "engineering:queue"
PROVISIONER_QUEUE = "provisioner:queue"
PROVISIONER_RESULTS = "provisioner:results"
ARCHITECT_QUEUE = "architect:queue"
SCAFFOLD_QUEUE = "scaffold:queue"
WORKER_COMMANDS = "worker:commands"
WORKER_RESPONSES = "worker:responses:developer"
PO_INPUT_QUEUE = "po:input"
PO_PROACTIVE_QUEUE = "po:proactive"
PO_REMINDERS_KEY = "po:reminders"

# ---------------------------------------------------------------------------
# Consumer group names
# ---------------------------------------------------------------------------
WORKER_GROUP = "capability-workers"
INFRA_GROUP = "infrastructure-workers"
SCHEDULER_CONSUMER_GROUP = "scheduler-consumers"
ARCHITECT_GROUP = "architect-consumers"
SCAFFOLD_GROUP = "scaffold-consumers"
TELEGRAM_BOT_GROUP = "telegram-bot"
WORKER_MANAGER_GROUP = "worker_manager"
PO_CONSUMER_GROUP = "po-consumer"
PO_PROACTIVE_GROUP = "tg-bot-proactive"

# ---------------------------------------------------------------------------
# Job retention TTL in seconds (7 days)
# ---------------------------------------------------------------------------
JOB_TTL_SECONDS = 7 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Declarative queue topology
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QueueBinding:
    """A single stream ↔ consumer-group binding."""

    stream: str
    group: str
    description: str


QUEUE_TOPOLOGY: list[QueueBinding] = [
    QueueBinding(SCAFFOLD_QUEUE, SCAFFOLD_GROUP, "Project scaffolding"),
    QueueBinding(ARCHITECT_QUEUE, ARCHITECT_GROUP, "Story → task decomposition"),
    QueueBinding(ENGINEERING_QUEUE, WORKER_GROUP, "Engineering tasks"),
    QueueBinding(DEPLOY_QUEUE, WORKER_GROUP, "Deploy tasks"),
    QueueBinding(PROVISIONER_QUEUE, INFRA_GROUP, "Server provisioning"),
    QueueBinding(PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP, "Provisioner results → scheduler"),
    QueueBinding(PROVISIONER_RESULTS, TELEGRAM_BOT_GROUP, "Provisioner results → telegram-bot"),
    QueueBinding(WORKER_COMMANDS, WORKER_MANAGER_GROUP, "Worker lifecycle commands"),
    QueueBinding(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "Product Owner input messages"),
    QueueBinding(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, "PO proactive messages → telegram-bot"),
]


async def ensure_all_groups(redis: Redis) -> None:
    """Create every consumer group declared in QUEUE_TOPOLOGY.

    Idempotent — silently skips groups that already exist.
    Should be called on worker startup.

    Args:
        redis: Connected Redis client
    """
    for binding in QUEUE_TOPOLOGY:
        try:
            await redis.xgroup_create(
                binding.stream,
                binding.group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "consumer_group_created",
                queue=binding.stream,
                group=binding.group,
            )
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.debug(
                    "consumer_group_exists",
                    queue=binding.stream,
                    group=binding.group,
                )
            else:
                logger.error(
                    "consumer_group_creation_failed",
                    queue=binding.stream,
                    group=binding.group,
                    error=str(e),
                )
                raise


# Backward-compatible alias
ensure_consumer_groups = ensure_all_groups
