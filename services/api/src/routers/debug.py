"""Debug endpoints for queue health inspection."""

from __future__ import annotations

from fastapi import APIRouter
import redis.asyncio as aioredis
import structlog

from shared.queues import QUEUE_TOPOLOGY

from ..config import get_settings

router = APIRouter(tags=["debug"])
logger = structlog.get_logger(__name__)


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
                    if pending > 100:  # noqa: PLR2004
                        issues.append(
                            f"High pending ({pending}) on {binding.stream}/{binding.group}"
                        )
                else:
                    issues.append(f"Group missing: {binding.group} on {binding.stream}")
            except Exception:  # noqa: S110
                # Stream doesn't exist → group can't exist either (already flagged above)
                pass

            bindings.append(entry)
    finally:
        await r.aclose()

    status = "degraded" if issues else "ok"
    return {"status": status, "bindings": bindings, "issues": issues}
