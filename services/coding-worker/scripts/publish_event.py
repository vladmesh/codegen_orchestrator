#!/usr/bin/env python3
"""Publish worker events to Redis pub/sub."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import sys

import redis

EVENTS_ALL_CHANNEL = "worker:events:all"
VALID_EVENT_TYPES = {"started", "progress", "completed", "failed"}
EXIT_USAGE = 2
MIN_ARGS = 2


def require_env(name: str) -> str:
    """Read required env var or fail fast."""

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def main() -> int:
    """Publish a single event using env-provided context."""

    if len(sys.argv) < MIN_ARGS:
        sys.stderr.write("Usage: publish_event.py <event_type>\n")
        return EXIT_USAGE

    event_type = sys.argv[1]
    if event_type not in VALID_EVENT_TYPES:
        sys.stderr.write(f"Invalid event_type: {event_type}\n")
        return EXIT_USAGE

    request_id = require_env("ORCHESTRATOR_REQUEST_ID")
    redis_url = require_env("ORCHESTRATOR_REDIS_URL")
    events_channel = require_env("ORCHESTRATOR_EVENTS_CHANNEL")
    worker_type = require_env("WORKER_TYPE")

    extra_raw = os.getenv("WORKER_EVENT_EXTRA")
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"WORKER_EVENT_EXTRA is invalid JSON: {exc}") from exc
    else:
        extra = {}

    event = {
        "request_id": request_id,
        "event_type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "worker_type": worker_type,
        **extra,
    }

    payload = json.dumps(event)
    client = redis.Redis.from_url(redis_url)
    try:
        client.publish(events_channel, payload)
        client.publish(EVENTS_ALL_CHANNEL, payload)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
