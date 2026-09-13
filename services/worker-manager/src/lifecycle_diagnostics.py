"""Read-only diagnostics for the worker-ownership rollout boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from shared.contracts.dto.worker import WORKER_TERMINAL_STATUSES
from shared.redis import decode_redis_fields, decode_redis_value

from .config import settings


class LifecycleRemainSummary(BaseModel):
    count: int
    oldest_age_seconds: int | None
    unknown_age_count: int
    identifiers: list[str]


class WorkerLifecycleDiagnostics(BaseModel):
    observed_at: datetime
    terminal_worker_remains: LifecycleRemainSummary
    ownerless_project_locks: LifecycleRemainSummary


def _summary(
    items: list[tuple[str, datetime | None]], observed_at: datetime
) -> LifecycleRemainSummary:
    ages = [
        max(0, int((observed_at - created_at).total_seconds()))
        for _, created_at in items
        if created_at
    ]
    return LifecycleRemainSummary(
        count=len(items),
        oldest_age_seconds=max(ages) if ages else None,
        unknown_age_count=sum(created_at is None for _, created_at in items),
        identifiers=sorted(identifier for identifier, _ in items),
    )


def _owned_at(meta: dict[str, str]) -> datetime | None:
    value = meta.get("owned_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _observed_creation(docker, worker_id: str) -> datetime | None:
    """Read a legacy worker's pre-existing Docker creation time when available."""
    try:
        inspected = await docker.inspect_container(f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}")
        value = inspected.get("Created")
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001 - unavailable Docker evidence means unknown age
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def collect_worker_lifecycle_diagnostics(
    redis, docker, *, observed_at: datetime | None = None
) -> WorkerLifecycleDiagnostics:
    """Count terminal remains and locks without a complete story owner."""
    observed_at = observed_at or datetime.now(UTC)
    terminal: list[tuple[str, datetime | None]] = []
    async for raw_key in redis.scan_iter(match="worker:status:*"):
        key = decode_redis_value(raw_key)
        worker_id = key.removeprefix("worker:status:")
        status = decode_redis_value(await redis.hget(key, "status"))
        if status not in WORKER_TERMINAL_STATUSES:
            continue
        meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
        created_at = _owned_at(meta) or await _observed_creation(docker, worker_id)
        terminal.append((worker_id, created_at))

    ownerless: list[tuple[str, datetime | None]] = []
    async for raw_key in redis.scan_iter(match="workspace:lock:*"):
        key = decode_redis_value(raw_key)
        project_id = key.removeprefix("workspace:lock:")
        worker_id = decode_redis_value(await redis.get(key))
        meta = (
            decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
            if worker_id
            else {}
        )
        if not meta.get("story_id"):
            created_at = _owned_at(meta)
            if created_at is None and worker_id:
                created_at = await _observed_creation(docker, worker_id)
            ownerless.append((project_id, created_at))

    return WorkerLifecycleDiagnostics(
        observed_at=observed_at,
        terminal_worker_remains=_summary(terminal, observed_at),
        ownerless_project_locks=_summary(ownerless, observed_at),
    )
