"""Redis Streams client with PEL reclaim, DLQ quarantine, and trimmed-entry diagnostics."""

import asyncio
from collections.abc import AsyncIterator
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
import time
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from redis.exceptions import TimeoutError as RedisTimeoutError
import structlog

from shared.contracts.recipient import (
    alert_legacy_recipient_field,
    has_legacy_recipient_field,
)
from shared.diagnostics import safe_validation_errors

try:
    import redis.asyncio as redis
except ImportError:
    redis = None  # type: ignore

logger = structlog.get_logger(__name__)


def decode_redis_value(value: Any) -> Any:
    """Normalize Redis bytes responses at the client boundary."""
    return value.decode() if isinstance(value, bytes) else value


def decode_redis_fields(fields: dict) -> dict[str, str]:
    """Decode a stream entry's field map to str keys and values."""
    return {decode_redis_value(k): decode_redis_value(v) for k, v in fields.items()}


@dataclass
class StreamMessage:
    """A message from a Redis Stream."""

    message_id: str
    data: dict[str, Any]
    # Reclaimed entries may still belong to a live worker.
    reclaimed: bool = False


@dataclass
class TypedMessage[T]:
    """A schema-validated Redis Stream message."""

    message_id: str
    value: T


DEFAULT_STREAM_MAXLEN = 1000

# Prevent a zero reclaim timeout from busy-looping XAUTOCLAIM.
MIN_RECLAIM_INTERVAL_MS = 1_000

# Durable diagnostic count for body-less trimmed PEL entries.
LOST_ENTRIES_KEY = "stream:diagnostics:lost_entries"

# Dead-letter stream suffix.
DLQ_SUFFIX = ":dlq"

DLQ_FAILURE_DECODE = "decode_error"
DLQ_FAILURE_VALIDATION = "validation_error"

# Redis 7 adds deleted IDs to XAUTOCLAIM's response.
_XAUTOCLAIM_WITH_DELETED_LEN = 3


def dlq_stream(stream: str) -> str:
    """The dead-letter stream that carries *stream*'s poison entries."""
    return f"{stream}{DLQ_SUFFIX}"


def resolve_reclaim_interval_ms(pending_timeout_ms: int, override: int | None = None) -> int:
    """Use the claim timeout as the sweep interval, with a nonzero floor."""
    if override is not None:
        return override
    return max(pending_timeout_ms, MIN_RECLAIM_INTERVAL_MS)


def _delete_at(data: Any, locs: list[list]) -> tuple[Any, list[str]]:
    """Copy data without fields named by validation locations."""
    pruned = copy.deepcopy(data)
    dropped: list[str] = []
    for loc in locs:
        container = pruned
        for step in loc[:-1]:
            if isinstance(container, dict) and step in container:
                container = container[step]
            elif isinstance(container, list) and isinstance(step, int) and step < len(container):
                container = container[step]
        if isinstance(container, dict) and loc[-1] in container:
            del container[loc[-1]]
            dropped.append(".".join(str(part) for part in loc))
    return pruned, dropped


def _candidate_of(loc: list, data: Any) -> Any:
    """Return the reporting union member, if a location names one."""
    if loc and isinstance(data, dict) and loc[0] not in data:
        return loc[0]
    return None


def unexpected_field_prunings(data: Any, exc: ValidationError) -> list[tuple[Any, list[str]]]:
    """Produce candidates only when unexpected fields are the sole error."""
    errors = exc.errors(include_url=False, include_input=False)
    if not errors or not isinstance(data, dict | list):
        return []

    by_candidate: dict[Any, list[tuple[str, list]]] = {}
    for error in errors:
        loc = list(error["loc"])
        if not loc:
            return []
        by_candidate.setdefault(_candidate_of(loc, data), []).append((error["type"], loc))

    prunings: list[tuple[Any, list[str]]] = []
    for reported in by_candidate.values():
        if any(error_type != "extra_forbidden" for error_type, _ in reported):
            continue
        pruned, dropped = _delete_at(data, [loc for _, loc in reported])
        if dropped:
            prunings.append((pruned, dropped))
    return prunings


def validate_tolerating_additions(adapter: TypeAdapter, data: Any) -> tuple[Any, list[str]]:
    """Accept only payloads made valid by dropping unknown fields."""
    try:
        return adapter.validate_python(data), []
    except ValidationError as exc:
        for candidate, dropped in unexpected_field_prunings(data, exc):
            try:
                return adapter.validate_python(candidate), dropped
            except ValidationError:
                continue
        raise


class RedisStreamClient:
    """Client for Redis Streams-based message passing."""

    def __init__(self, redis_url: str | None = None, *, stream_maxlen: int = DEFAULT_STREAM_MAXLEN):
        """Initialize Redis client.

        Args:
            redis_url: Redis connection URL. Falls back to REDIS_URL env var.
            stream_maxlen: Approximate max messages per stream (MAXLEN ~). 0 to disable.
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        if not self.redis_url:
            raise RuntimeError(
                "Redis URL not provided. Pass redis_url argument or set REDIS_URL env var."
            )
        self._redis: redis.Redis | None = None
        self._stream_maxlen = stream_maxlen

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._redis is None:
            if redis is None:
                raise ImportError("redis package is not installed.")
            # Blocking stream reads use their own ``block`` interval. Disable the
            # transport read timeout so an idle XREADGROUP is not mistaken for a
            # dead consumer when REDIS_URL carries a shorter socket_timeout.
            self._redis = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_timeout=None,
            )
            logger.info("redis_connected")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("redis_connection_closed")

    @property
    def redis(self) -> "redis.Redis":
        """Get Redis client, ensuring connection."""
        if self._redis is None:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._redis

    def _xadd_kwargs(self) -> dict[str, Any]:
        """Return maxlen kwargs for xadd if configured."""
        if self._stream_maxlen:
            return {"maxlen": self._stream_maxlen, "approximate": True}
        return {}

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        """Publish a dict to a Redis Stream (wrapped in JSON 'data' field)."""
        message = {"data": json.dumps(data)}
        message_id = await self.redis.xadd(stream, message, **self._xadd_kwargs())
        logger.debug("message_published", stream=stream, message_id=message_id)
        return message_id

    async def publish_flat(self, stream: str, fields: dict[str, str]) -> str:
        """Publish flat key-value fields directly to a Redis Stream (no JSON wrapping)."""
        message_id = await self.redis.xadd(stream, fields, **self._xadd_kwargs())
        logger.debug("message_published_flat", stream=stream, message_id=message_id)
        return message_id

    async def publish_message(self, stream: str, message: BaseModel) -> str:
        """Publish a Pydantic model to a Redis Stream."""
        data = message.model_dump(mode="json")
        return await self.publish(stream, data)

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message, removing it from the pending entries list (PEL)."""
        await self.redis.xack(stream, group, message_id)
        logger.debug("message_acked", stream=stream, message_id=message_id)

    async def delivery_count(self, stream: str, group: str, message_id: str) -> int:
        """How many times this group has been handed *message_id*.

        Read from the group's PEL, so the count survives a consumer restart:
        a handler that must bound its attempts across restarts cannot keep the
        tally in memory. Returns 0 once the entry is acked and gone from the PEL.
        """
        entries = await self.redis.xpending_range(
            stream, group, min=message_id, max=message_id, count=1
        )
        if not entries:
            return 0
        return int(entries[0]["times_delivered"])

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        """Ensure a consumer group exists for the stream.

        Uses id="0" to process ALL messages including ones sent before group creation.
        This prevents race conditions where messages are sent before worker starts.
        """
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("consumer_group_created", stream=stream, group=group)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("consumer_group_exists", stream=stream, group=group)
            else:
                raise

    @staticmethod
    def _parse_fields(fields: dict[str, str]) -> dict[str, Any]:
        """Parse Redis stream message fields into a data dict.

        Handles two formats:
        - Wrapped: {"data": "<JSON string>"} → parsed JSON dict
        - Flat: {"key1": "val1", "key2": "val2"} → fields as-is
        """
        fields = decode_redis_fields(fields)
        if "data" in fields:
            try:
                parsed = json.loads(fields["data"])
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return dict(fields)

    @staticmethod
    def _decode_entry(fields: dict[str, str]) -> Any:
        """Strict decode for the typed consume path.

        Unlike ``_parse_fields`` (which silently falls back to the flat field
        map when the wrapped ``data`` payload is malformed), this raises
        ``json.JSONDecodeError`` so ``consume_typed`` can surface a broken
        payload as a terminal error instead of swallowing it.
        """
        fields = decode_redis_fields(fields)
        if "data" in fields:
            return json.loads(fields["data"])
        return dict(fields)

    async def _record_lost_entry(self, stream: str, group: str, entry_id: str | None) -> None:
        """Count and log a pending entry the stream no longer holds.

        XAUTOCLAIM reports an entry with no body when the entry itself is gone
        from the stream: every publish carries ``MAXLEN ~`` and the scheduler
        additionally runs ``XTRIM MINID`` by age, and neither of them looks at
        the PEL first. The work the entry described cannot be recovered — but a
        trim that eats live work must not read as an idle queue, so it is
        counted where the count outlives this process.
        """
        lost_total = await self.redis.hincrby(LOST_ENTRIES_KEY, f"{stream}|{group}", 1)
        logger.error(
            "stream_entry_lost_to_trim",
            stream=stream,
            group=group,
            entry_id=entry_id,
            lost_total=lost_total,
        )

    async def lost_entry_count(self, stream: str, group: str) -> int:
        """Entries this group lost to a trim, as counted by ``_record_lost_entry``."""
        counted = await self.redis.hget(LOST_ENTRIES_KEY, f"{stream}|{group}")
        if counted is None:
            return 0
        return int(decode_redis_value(counted))

    async def _reclaim_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        pending_timeout_ms: int,
    ) -> AsyncIterator[tuple[str, dict[str, str]]]:
        """One XAUTOCLAIM sweep over the group's PEL.

        ``min_idle_time=pending_timeout_ms`` is the whole protection against
        taking work away from a healthy consumer, so it is passed through
        untouched: an entry is only claimable once nobody has been handed it for
        that long.
        """
        cursor = "0-0"
        while True:
            result = await self.redis.xautoclaim(
                stream,
                group,
                consumer,
                min_idle_time=pending_timeout_ms,
                start_id=cursor,
                count=count,
            )
            new_cursor = decode_redis_value(result[0])
            claimed = result[1]
            # Redis 7 reports trimmed IDs separately; Redis 6.2 returns body-less entries.
            deleted = result[2] if len(result) >= _XAUTOCLAIM_WITH_DELETED_LEN else []

            for message_id, fields in claimed:
                if fields is None:
                    await self._record_lost_entry(
                        stream, group, decode_redis_value(message_id) if message_id else None
                    )
                    continue
                yield decode_redis_value(message_id), fields
            for message_id in deleted:
                await self._record_lost_entry(stream, group, decode_redis_value(message_id))

            # Advance through fresh entries; only a terminal cursor ends the PEL scan.
            if new_cursor in ("0-0", cursor):
                break
            cursor = new_cursor

    async def _iter_entries(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int,
        count: int,
        claim_pending: bool,
        pending_timeout_ms: int,
        reclaim_interval_ms: int,
    ) -> AsyncIterator[tuple[str, dict[str, str], bool] | None]:
        """Alternate recurring PEL reclaim and blocking reads without ACKing."""
        await self.ensure_consumer_group(stream, group)
        reclaim_interval_s = reclaim_interval_ms / 1000
        next_reclaim_at = 0.0

        while True:
            try:
                if claim_pending and time.monotonic() >= next_reclaim_at:
                    # A failed sweep waits for its next scheduled interval.
                    next_reclaim_at = time.monotonic() + reclaim_interval_s
                    async for entry in self._reclaim_pending(
                        stream, group, consumer, count, pending_timeout_ms
                    ):
                        message_id, fields = entry
                        yield message_id, fields, True

                messages = await self.redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=count,
                    block=block_ms,
                )

                if not messages:
                    # Prevent immediate-return backends from busy-spinning.
                    await asyncio.sleep(0)
                    yield None
                    continue

                for _stream_name, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        yield decode_redis_value(message_id), fields, False

            except asyncio.CancelledError:
                logger.info("consumer_cancelled", consumer=consumer)
                break
            except RedisTimeoutError:
                # A blocking read reaching a transport timeout is an idle poll,
                # not a fatal consumer error. Yield control and keep polling.
                await asyncio.sleep(0)
                yield None
            except Exception as e:
                if "NOGROUP" in str(e):
                    logger.warning("consumer_nogroup_recovering", stream=stream, group=group)
                    await self.ensure_consumer_group(stream, group)
                else:
                    logger.error("consume_error", stream=stream, error=str(e))
                await asyncio.sleep(1)

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
        count: int = 1,
        auto_ack: bool = True,
        claim_pending: bool = False,
        pending_timeout_ms: int = 60_000,
        reclaim_interval_ms: int | None = None,
    ) -> AsyncIterator[StreamMessage]:
        """Consume messages from a Redis Stream using consumer groups.

        Args:
            stream: Stream name to consume from.
            group: Consumer group name.
            consumer: Consumer name within the group.
            block_ms: How long to block waiting for messages (ms).
            count: Number of messages to attempt reading per iteration.
            auto_ack: If True, messages are ACKed immediately after yield.
                      If False, caller must call ack() manually.
            claim_pending: If True, recover pending messages (PEL) repeatedly
                for as long as this consumer lives, not only at start-up.
            pending_timeout_ms: Min idle time (ms) for XAUTOCLAIM to claim pending messages.
            reclaim_interval_ms: Time between XAUTOCLAIM sweeps. Defaults to
                ``pending_timeout_ms``, floored at ``MIN_RECLAIM_INTERVAL_MS``.
        """
        async for entry in self._iter_entries(
            stream,
            group,
            consumer,
            block_ms,
            count,
            claim_pending,
            pending_timeout_ms,
            resolve_reclaim_interval_ms(pending_timeout_ms, reclaim_interval_ms),
        ):
            if entry is None:
                yield None  # type: ignore[misc]
                continue
            message_id, fields, reclaimed = entry
            data = self._parse_fields(fields)
            yield StreamMessage(message_id=message_id, data=data, reclaimed=reclaimed)
            if auto_ack:
                await self.redis.xack(stream, group, message_id)
                logger.debug("message_acked", message_id=message_id)

    async def _terminal_ack(self, stream: str, group: str, message_id: str) -> None:
        """ACK poison entries without stopping the consumer on transient failure."""
        try:
            await self.redis.xack(stream, group, message_id)
        except Exception as e:
            logger.error(
                "typed_consume_terminal_ack_failed",
                stream=stream,
                entry_id=message_id,
                error=str(e),
            )

    async def _quarantine_entry(
        self,
        stream: str,
        group: str,
        message_id: str,
        *,
        fields: dict[str, str],
        failure: str,
        reason: Any,
    ) -> bool:
        """Copy poison entries to the same Redis DLQ without logging their bodies."""
        target = dlq_stream(stream)
        entry = {
            "source_stream": stream,
            "group": group,
            "entry_id": message_id,
            "failure": failure,
            "reason": json.dumps(reason),
            "quarantined_at": datetime.now(UTC).isoformat(),
            "body": json.dumps(decode_redis_fields(fields)),
        }
        try:
            dlq_id = await self.redis.xadd(target, entry, **self._xadd_kwargs())
        except Exception as e:
            # Payload-handling failures log only their type because bodies may contain secrets.
            logger.error(
                "typed_consume_quarantine_failed",
                stream=stream,
                dlq=target,
                entry_id=message_id,
                failure=failure,
                error_type=type(e).__name__,
            )
            return False
        logger.error(
            "typed_consume_entry_quarantined",
            stream=stream,
            dlq=target,
            entry_id=message_id,
            dlq_entry_id=decode_redis_value(dlq_id),
            failure=failure,
        )
        return True

    async def _reject_entry(
        self,
        stream: str,
        group: str,
        message_id: str,
        *,
        fields: dict[str, str],
        failure: str,
        reason: Any,
    ) -> None:
        """ACK poison entries only after their DLQ copy succeeds."""
        if await self._quarantine_entry(
            stream, group, message_id, fields=fields, failure=failure, reason=reason
        ):
            await self._terminal_ack(stream, group, message_id)

    async def consume_typed[T](
        self,
        stream: str,
        group: str,
        consumer: str,
        message_type: type[T] | TypeAdapter,
        *,
        block_ms: int = 5000,
        count: int = 1,
        claim_pending: bool = True,
        pending_timeout_ms: int = 60_000,
        reclaim_interval_ms: int | None = None,
    ) -> AsyncIterator["TypedMessage[T] | None"]:
        """Validate typed messages; quarantine terminal errors before ACKing."""
        adapter = (
            message_type if isinstance(message_type, TypeAdapter) else TypeAdapter(message_type)
        )

        async for entry in self._iter_entries(
            stream,
            group,
            consumer,
            block_ms,
            count,
            claim_pending,
            pending_timeout_ms,
            resolve_reclaim_interval_ms(pending_timeout_ms, reclaim_interval_ms),
        ):
            if entry is None:
                yield None
                continue
            message_id, fields, _reclaimed = entry

            try:
                data = self._decode_entry(fields)
            except json.JSONDecodeError as e:
                # Never log raw fields: queue payloads may contain secrets.
                logger.error(
                    "typed_consume_decode_failed",
                    stream=stream,
                    entry_id=message_id,
                    error=str(e),
                )
                await self._reject_entry(
                    stream,
                    group,
                    message_id,
                    fields=fields,
                    failure=DLQ_FAILURE_DECODE,
                    reason={"error": str(e)},
                )
                continue

            try:
                value, dropped = validate_tolerating_additions(adapter, data)
            except ValidationError as e:
                # Validation input may contain secrets, so log only safe errors.
                errors = safe_validation_errors(e)
                logger.error(
                    "typed_consume_validation_failed",
                    stream=stream,
                    entry_id=message_id,
                    errors=errors,
                )
                # A message still addressed by the removed ``user_id`` field is
                # not just malformed: somebody's notification has nowhere to go.
                # Reject it loudly instead of letting it pass as unaddressable
                # work.
                if has_legacy_recipient_field(data):
                    await alert_legacy_recipient_field(
                        source=stream, entry_id=message_id, data=data
                    )
                await self._reject_entry(
                    stream,
                    group,
                    message_id,
                    fields=fields,
                    failure=DLQ_FAILURE_VALIDATION,
                    reason=errors,
                )
                continue

            if dropped:
                # Field names only. They come from the validation error's ``loc``,
                # which is already what the elided error log carries; no value
                # from the payload is named here.
                logger.warning(
                    "typed_consume_unknown_fields_ignored",
                    stream=stream,
                    entry_id=message_id,
                    unknown_fields=dropped,
                )

            yield TypedMessage(message_id=message_id, value=value)
