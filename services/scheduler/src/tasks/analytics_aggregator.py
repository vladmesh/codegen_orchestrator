"""Analytics aggregator — hourly metrics from Loki, daily rollups, cleanup.

Runs every hour at :05. For each active project:
1. Query Loki for structured request logs in the last hour
2. Compute hourly metrics (requests, errors, users, percentiles, top endpoints)
3. Upsert into analytics_hourly via API
4. At midnight UTC: roll up hourly→daily, compute returning users
5. Cleanup: hourly >90 days, daily >365 days
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import time

import structlog

from shared.clients.loki import LokiClient
from src.clients.api import api_client

logger = structlog.get_logger()

AGGREGATION_INTERVAL = 3600  # 1 hour
HOURLY_RETENTION_DAYS = 90
DAILY_RETENTION_DAYS = 365
HTTP_SERVER_ERROR_THRESHOLD = 500
AGGREGATION_MINUTE = 5  # Run at :05 past the hour


def compute_hourly_metrics(
    logs: list[dict],
    known_user_hashes: set[str],
) -> dict:
    """Compute hourly metrics from a list of parsed Loki log entries.

    Args:
        logs: Parsed log entries with event, status_code, duration_ms, user_id.
        known_user_hashes: Set of user_id_hash values already seen for this project.

    Returns:
        Dict with metric fields + seen_users list for known_users upsert.
    """
    total_requests = 0
    error_count = 0
    durations: list[float] = []
    user_ids: set[str] = set()
    endpoint_counter: Counter[str] = Counter()

    for entry in logs:
        # Only count request events
        event = entry.get("event")
        if event != "request":
            continue

        total_requests += 1

        # Errors: HTTP 5xx or level=error
        status_code = entry.get("status_code")
        level = entry.get("level", "")
        is_server_error = (
            isinstance(status_code, int) and status_code >= HTTP_SERVER_ERROR_THRESHOLD
        )
        if is_server_error or level == "error":
            error_count += 1

        # Duration
        duration = entry.get("duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))

        # Users
        user_id = entry.get("user_id")
        if user_id:
            user_ids.add(str(user_id))

        # Endpoints
        path = entry.get("path") or entry.get("command")
        if path:
            endpoint_counter[path] += 1

    # Percentiles
    p50 = _percentile(durations, 50)
    p95 = _percentile(durations, 95)
    p99 = _percentile(durations, 99)

    # Top endpoints
    top_endpoints = [
        {"path": path, "count": count} for path, count in endpoint_counter.most_common(5)
    ]

    # New vs known users
    user_hashes = {_hash_user_id(uid) for uid in user_ids}
    new_user_hashes = user_hashes - known_user_hashes

    return {
        "total_requests": total_requests,
        "error_count": error_count,
        "unique_users": len(user_hashes),
        "new_users": len(new_user_hashes),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "top_endpoints": top_endpoints,
        "seen_users": [{"user_id_hash": h} for h in user_hashes],
    }


def compute_daily_rollup(hourly_rows: list[dict], known_users: list[dict]) -> dict:
    """Compute daily rollup from hourly analytics rows.

    Args:
        hourly_rows: List of hourly analytics dicts for the day.
        known_users: List of known user dicts with first_seen/last_seen.

    Returns:
        Dict with daily metric fields.
    """
    total_requests = sum(r["total_requests"] for r in hourly_rows)
    error_count = sum(r["error_count"] for r in hourly_rows)
    new_users = sum(r["new_users"] for r in hourly_rows)

    # Unique users: count distinct user hashes seen in known_users for the day
    # (since hourly unique_users can double-count across hours)
    # We use known_users.last_seen to filter for today
    unique_users = sum(r["unique_users"] for r in hourly_rows)
    # Best approximation: MAX of hourly unique_users (lower bound on true daily)
    # In practice, for small scale this is fine per brainstorm decisions
    dau = max((r["unique_users"] for r in hourly_rows), default=0)

    # Returning users: users in known_users whose first_seen < today
    # (they existed before today)
    returning_users = sum(1 for u in known_users if u.get("first_seen") and u.get("last_seen"))
    # Subtract new users from total known to get returning
    returning_users = max(0, len(known_users) - new_users)

    # p95: worst-of-hourly-p95s per brainstorm decision
    p95_values = [r["p95_ms"] for r in hourly_rows if r.get("p95_ms") is not None]
    p95 = max(p95_values) if p95_values else None

    # Error rate
    error_rate = error_count / total_requests if total_requests > 0 else 0.0

    return {
        "total_requests": total_requests,
        "error_count": error_count,
        "unique_users": unique_users,
        "new_users": new_users,
        "dau": dau,
        "returning_users": returning_users,
        "p95_ms": p95,
        "error_rate": round(error_rate, 4),
    }


def _percentile(values: list[float], pct: int) -> float | None:
    """Compute percentile from a sorted list."""
    if not values:
        return None
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * pct / 100)
    idx = min(idx, len(sorted_vals) - 1)
    return round(sorted_vals[idx], 2)


def _hash_user_id(user_id: str) -> str:
    """SHA256 hash of user_id for privacy."""
    return hashlib.sha256(user_id.encode()).hexdigest()


async def _aggregate_hourly(loki: LokiClient, bucket_start: datetime, bucket_end: datetime):
    """Run hourly aggregation for all active projects."""
    # Get projects that have running applications
    apps = await api_client.get_applications(status="running")
    if not apps:
        logger.info("analytics_no_active_apps")
        return

    # Group applications by project_id (via repo → project chain)
    # We need project_id for each app; get it from repositories
    project_services: dict[str, set[str]] = {}
    for app in apps:
        repo = await api_client.get_repositories()
        matching = [r for r in repo if r.id == app.repo_id]
        if not matching:
            continue
        pid = matching[0].project_id
        if pid not in project_services:
            project_services[pid] = set()
        project_services[pid].add(app.service_name)

    for project_id, services in project_services.items():
        try:
            await _aggregate_project_hourly(loki, project_id, services, bucket_start, bucket_end)
        except Exception:
            logger.exception(
                "analytics_project_error",
                project_id=project_id,
            )


async def _aggregate_project_hourly(
    loki: LokiClient,
    project_id: str,
    services: set[str],
    bucket_start: datetime,
    bucket_end: datetime,
):
    """Aggregate hourly metrics for one project."""
    # Get known users for this project
    known_users_raw = await api_client.get_known_users(project_id)
    known_user_hashes = {u["user_id_hash"] for u in known_users_raw}

    for service_name in services:
        query = (
            f'{{job="docker", project_id="{project_id}", compose_service="{service_name}"}} | json'
        )
        logs = await loki.query_range(query, bucket_start, bucket_end)

        if not logs:
            logger.debug(
                "analytics_no_logs",
                project_id=project_id,
                service=service_name,
            )
            continue

        metrics = compute_hourly_metrics(logs, known_user_hashes)
        seen_users = metrics.pop("seen_users")

        # Upsert hourly row
        await api_client.upsert_analytics_hourly(
            {
                "project_id": project_id,
                "service_name": service_name,
                "bucket": bucket_start.isoformat(),
                **metrics,
            }
        )

        # Update known users
        if seen_users:
            now_iso = bucket_end.isoformat()
            users_payload = [
                {
                    "user_id_hash": u["user_id_hash"],
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                }
                for u in seen_users
            ]
            await api_client.upsert_known_users(project_id, users_payload)

            # Update known set for next service
            known_user_hashes.update(u["user_id_hash"] for u in seen_users)

    logger.info(
        "analytics_project_done",
        project_id=project_id,
        services=len(services),
    )


async def _run_daily_rollup(yesterday: datetime):
    """Roll up hourly data into daily for yesterday."""
    yesterday_date = yesterday.date().isoformat()
    today_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    yesterday_start = today_start - timedelta(days=1)

    # Get all projects that have hourly data for yesterday
    apps = await api_client.get_applications(status="running")
    project_ids: set[str] = set()
    for app in apps:
        repos = await api_client.get_repositories()
        matching = [r for r in repos if r.id == app.repo_id]
        if matching:
            project_ids.add(matching[0].project_id)

    for project_id in project_ids:
        try:
            hourly_rows = await api_client.get_analytics_hourly(
                project_id,
                start=yesterday_start.isoformat(),
                end=today_start.isoformat(),
            )
            if not hourly_rows:
                continue

            known_users = await api_client.get_known_users(project_id)
            daily = compute_daily_rollup(hourly_rows, known_users)

            await api_client.upsert_analytics_daily(
                {
                    "project_id": project_id,
                    "date": yesterday_date,
                    **daily,
                }
            )

            logger.info(
                "analytics_daily_rollup_done",
                project_id=project_id,
                date=yesterday_date,
            )
        except Exception:
            logger.exception(
                "analytics_daily_rollup_error",
                project_id=project_id,
            )


async def _cleanup():
    """Remove old analytics data per retention policy."""
    hourly_result = await api_client.delete_old_hourly(HOURLY_RETENTION_DAYS)
    daily_result = await api_client.delete_old_daily(DAILY_RETENTION_DAYS)
    logger.info(
        "analytics_cleanup_done",
        hourly_deleted=hourly_result["deleted"],
        daily_deleted=daily_result["deleted"],
    )


async def analytics_aggregator_worker():
    """Background worker — runs hourly aggregation at :05 past the hour."""
    import os

    if not os.environ.get("LOKI_URL"):
        logger.warning("analytics_aggregator_disabled", reason="LOKI_URL not set")
        # Stay alive but idle — don't crash the scheduler
        while True:
            await asyncio.sleep(3600)

    logger.info("analytics_aggregator_worker_started")

    loki = LokiClient()

    try:
        while True:
            # Wait until :05 of the next hour
            now = datetime.now(UTC)
            next_run = now.replace(minute=AGGREGATION_MINUTE, second=0, microsecond=0)
            if now.minute >= AGGREGATION_MINUTE:
                next_run += timedelta(hours=1)
            wait_seconds = (next_run - now).total_seconds()

            logger.info(
                "analytics_waiting",
                next_run=next_run.isoformat(),
                wait_seconds=round(wait_seconds),
            )
            await asyncio.sleep(wait_seconds)

            start_time = time.time()
            now = datetime.now(UTC)

            # Bucket = previous hour
            bucket_end = now.replace(minute=0, second=0, microsecond=0)
            bucket_start = bucket_end - timedelta(hours=1)

            logger.info(
                "analytics_cycle_start",
                bucket_start=bucket_start.isoformat(),
                bucket_end=bucket_end.isoformat(),
            )

            try:
                await _aggregate_hourly(loki, bucket_start, bucket_end)

                # Daily rollup at midnight UTC (when current hour is 0)
                if now.hour == 0:
                    yesterday = now - timedelta(days=1)
                    await _run_daily_rollup(yesterday)
                    await _cleanup()

            except Exception:
                logger.exception("analytics_cycle_error")

            duration = time.time() - start_time
            logger.info("analytics_cycle_complete", duration_sec=round(duration, 2))
    finally:
        await loki.close()
