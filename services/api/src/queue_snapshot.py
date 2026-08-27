"""Single owner for the Redis queue-health snapshot shown to operators."""

import redis.asyncio as aioredis
import structlog

from shared.contracts.dto.admin_overview import (
    QueueBindingSnapshot,
    QueueGroupInfo,
    QueueHealthSnapshot,
    QueueStreamInfo,
)
from shared.queues import QUEUE_TOPOLOGY
from shared.redis import decode_redis_fields

from .config import get_settings

logger = structlog.get_logger(__name__)

HIGH_PENDING_THRESHOLD = 100


def _is_missing_redis_object(error: Exception) -> bool:
    message = str(error).lower()
    return "no such key" in message or "nogroup" in message


async def get_queue_snapshot() -> QueueHealthSnapshot:
    """Inspect every declared binding without turning absent data into zeroes."""
    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    bindings: list[QueueBindingSnapshot] = []
    issues: list[str] = []

    try:
        for binding in QUEUE_TOPOLOGY:
            stream_info: QueueStreamInfo | None = None
            group_info: QueueGroupInfo | None = None
            try:
                info = await redis.xinfo_stream(binding.stream)
                stream_info = QueueStreamInfo(length=info["length"])
            except Exception as error:  # Redis errors are operational state, not a false zero.
                kind = "missing" if _is_missing_redis_object(error) else "error"
                issues.append(f"Stream {kind}: {binding.stream}: {error}")

            try:
                groups = [
                    decode_redis_fields(group) for group in await redis.xinfo_groups(binding.stream)
                ]
                group = next((item for item in groups if item.get("name") == binding.group), None)
                if group is None:
                    issues.append(f"Group missing: {binding.group} on {binding.stream}")
                else:
                    group_info = QueueGroupInfo(
                        consumers=group.get("consumers", 0),
                        pending=group.get("pending", 0),
                        last_delivered_id=group.get("last-delivered-id", "0-0"),
                    )
                    if group_info.pending > HIGH_PENDING_THRESHOLD:
                        issues.append(
                            f"High pending ({group_info.pending}) on "
                            f"{binding.stream}/{binding.group}"
                        )
            except Exception as error:  # Partial group data must remain visible as degraded.
                logger.warning(
                    "queue_group_check_failed",
                    stream=binding.stream,
                    group=binding.group,
                    error=str(error),
                )
                kind = "missing" if _is_missing_redis_object(error) else "error"
                issues.append(f"Group {kind}: {binding.group} on {binding.stream}: {error}")

            bindings.append(
                QueueBindingSnapshot(
                    stream=binding.stream,
                    group=binding.group,
                    description=binding.description,
                    stream_info=stream_info,
                    group_info=group_info,
                )
            )
    finally:
        await redis.aclose()

    return QueueHealthSnapshot(
        status="degraded" if issues else "ok", bindings=bindings, issues=issues
    )
