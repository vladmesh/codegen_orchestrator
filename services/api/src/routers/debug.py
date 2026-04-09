"""Debug endpoints for queue health inspection."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, HTTPException, Query
import redis.asyncio as aioredis
import structlog

from shared.queues import QUEUE_TOPOLOGY

from ..config import get_settings

router = APIRouter(tags=["debug"])
logger = structlog.get_logger(__name__)

HIGH_PENDING_THRESHOLD = 100


@router.get("/debug/queues")
async def debug_queues() -> dict:
    """Return health status of every declared queue binding.

    For each entry in QUEUE_TOPOLOGY, reports stream length,
    consumer-group info, and flags issues (missing groups, high pending).
    """
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)

    bindings: list[dict] = []
    issues: list[str] = []

    try:
        for binding in QUEUE_TOPOLOGY:
            entry: dict = {
                "stream": binding.stream,
                "group": binding.group,
                "description": binding.description,
                "stream_info": None,
                "group_info": None,
            }

            # Stream info
            try:
                sinfo = await r.xinfo_stream(binding.stream)
                entry["stream_info"] = {"length": sinfo.get("length", 0)}
            except Exception as e:
                if "no such key" in str(e).lower() or "ERR" in str(e):
                    entry["stream_info"] = {"length": 0}
                    issues.append(f"Stream missing: {binding.stream}")
                else:
                    issues.append(f"Stream error ({binding.stream}): {e}")

            # Group info
            try:
                groups = await r.xinfo_groups(binding.stream)
                matched = [g for g in groups if g.get("name") == binding.group]
                if matched:
                    g = matched[0]
                    pending = g.get("pending", 0)
                    entry["group_info"] = {
                        "consumers": g.get("consumers", 0),
                        "pending": pending,
                        "last_delivered_id": g.get("last-delivered-id", "0-0"),
                    }
                    if pending > HIGH_PENDING_THRESHOLD:
                        issues.append(
                            f"High pending ({pending}) on {binding.stream}/{binding.group}"
                        )
                else:
                    issues.append(f"Group missing: {binding.group} on {binding.stream}")
            except aioredis.ResponseError:
                logger.warning(
                    "queue_group_check_failed",
                    stream=binding.stream,
                    group=binding.group,
                )

            bindings.append(entry)
    finally:
        await r.aclose()

    status = "degraded" if issues else "ok"
    return {"status": status, "bindings": bindings, "issues": issues}


def _parse_message_id(mid: str) -> float:
    """Convert Redis stream message ID (e.g. '1710000000000-0') to epoch seconds."""
    try:
        ts_ms = int(mid.split("-")[0])
        return ts_ms / 1000.0
    except (ValueError, IndexError):
        return 0.0


def _parse_fields(fields: dict) -> dict:
    """Unwrap the {data: json_string} envelope if present, otherwise return raw."""
    import json

    if "data" in fields and len(fields) == 1:
        try:
            return json.loads(fields["data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return fields


@router.get("/debug/queues/{stream}/messages")
async def queue_messages(
    stream: str,
    count: int = Query(default=50, ge=1, le=500),
    start: str = Query(default="-"),
    end: str = Query(default="+"),
) -> dict:
    """List messages in a Redis stream (XRANGE).

    Returns messages oldest-first with parsed data and timestamps.
    """
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        try:
            raw = await r.xrange(stream, min=start, max=end, count=count)
        except Exception as e:
            if "no such key" in str(e).lower() or "ERR" in str(e):
                return {"stream": stream, "messages": [], "total": 0}
            raise

        messages = []
        for mid, fields in raw:
            messages.append(
                {
                    "id": mid,
                    "timestamp": _parse_message_id(mid),
                    "data": _parse_fields(fields),
                    "raw_fields": fields,
                }
            )

        # Get total stream length
        try:
            info = await r.xinfo_stream(stream)
            total = info.get("length", len(messages))
        except Exception:
            total = len(messages)

        return {"stream": stream, "messages": messages, "total": total}
    finally:
        await r.aclose()


@router.get("/debug/queues/{stream}/{group}/pending")
async def queue_pending(
    stream: str,
    group: str,
    count: int = Query(default=50, ge=1, le=500),
) -> dict:
    """List pending messages for a consumer group (XPENDING).

    Shows messages that were delivered but not yet acknowledged.
    """
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        try:
            raw = await r.xpending_range(stream, group, min="-", max="+", count=count)
        except Exception as e:
            if "no such key" in str(e).lower() or "NOGROUP" in str(e):
                return {"stream": stream, "group": group, "pending": []}
            raise

        pending = []
        for entry in raw:
            pending.append(
                {
                    "id": entry.get("message_id", ""),
                    "consumer": entry.get("consumer", ""),
                    "idle_ms": entry.get("time_since_delivered", 0),
                    "delivery_count": entry.get("times_delivered", 0),
                }
            )

        return {"stream": stream, "group": group, "pending": pending}
    finally:
        await r.aclose()


@router.post("/debug/queues/{stream}/{group}/ack/{message_id:path}")
async def queue_ack_message(stream: str, group: str, message_id: str) -> dict:
    """Acknowledge a pending message."""
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        acked = await r.xack(stream, group, message_id)
        if not acked:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Message {message_id} not found or already acknowledged",
            )
        return {"acknowledged": True, "stream": stream, "group": group, "id": message_id}
    finally:
        await r.aclose()


@router.delete("/debug/queues/{stream}/messages/{message_id:path}")
async def queue_delete_message(stream: str, message_id: str) -> dict:
    """Delete a message from a stream (XDEL)."""
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        deleted = await r.xdel(stream, message_id)
        if not deleted:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Message {message_id} not found in stream {stream}",
            )
        return {"deleted": True, "stream": stream, "id": message_id}
    finally:
        await r.aclose()
