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
SCAFFOLDER_QUEUE = "scaffolder:queue"
PROVISIONER_QUEUE = "provisioner:queue"
PROVISIONER_RESULTS = "provisioner:results"
ANSIBLE_DEPLOY_QUEUE = "ansible:deploy:queue"
WORKER_COMMANDS = "worker:commands"
PO_INPUT_QUEUE = "po:input"

# ---------------------------------------------------------------------------
# Consumer group names
# ---------------------------------------------------------------------------
WORKER_GROUP = "capability-workers"
SCAFFOLDER_GROUP = "scaffolder-workers"
INFRA_GROUP = "infrastructure-workers"
SCHEDULER_CONSUMER_GROUP = "scheduler-consumers"
TELEGRAM_BOT_GROUP = "telegram-bot"
WORKER_MANAGER_GROUP = "worker_manager"
PO_CONSUMER_GROUP = "po-consumer"

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
    QueueBinding(ENGINEERING_QUEUE, WORKER_GROUP, "Engineering tasks"),
    QueueBinding(DEPLOY_QUEUE, WORKER_GROUP, "Deploy tasks"),
    QueueBinding(SCAFFOLDER_QUEUE, SCAFFOLDER_GROUP, "Project scaffolding"),
    QueueBinding(PROVISIONER_QUEUE, INFRA_GROUP, "Server provisioning"),
    QueueBinding(ANSIBLE_DEPLOY_QUEUE, INFRA_GROUP, "Ansible deployments"),
    QueueBinding(PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP, "Provisioner results → scheduler"),
    QueueBinding(PROVISIONER_RESULTS, TELEGRAM_BOT_GROUP, "Provisioner results → telegram-bot"),
    QueueBinding(WORKER_COMMANDS, WORKER_MANAGER_GROUP, "Worker lifecycle commands"),
    QueueBinding(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "Product Owner input messages"),
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
