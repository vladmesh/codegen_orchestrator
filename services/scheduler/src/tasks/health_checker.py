"""Health checker worker — polls node_exporter + cadvisor via HTTP.

Monitors managed servers, updates metrics in DB, creates incidents
on failure or resource exhaustion, and notifies admins via Telegram.
"""

import asyncio
from datetime import UTC, datetime
import os
import time

import httpx
import structlog

from shared.contracts.dto.server import ServerUpdate
from shared.models.incident import IncidentType
from shared.notifications import notify_admins
from src.clients.api import api_client
from src.metrics import parse_cadvisor, parse_node_exporter
from src.tasks.app_health_prober import app_health_probe_cycle

logger = structlog.get_logger()

# Configuration
_interval = os.getenv("HEALTH_CHECK_INTERVAL")
if not _interval:
    raise RuntimeError("HEALTH_CHECK_INTERVAL is not set")
HEALTH_CHECK_INTERVAL = int(_interval)

NODE_EXPORTER_PORT = 9100
CADVISOR_PORT = 8080
HTTP_TIMEOUT = 10.0

# Thresholds for resource exhaustion alerts
RAM_THRESHOLD_PCT = 90.0
DISK_THRESHOLD_PCT = 90.0

# Cleanup: run once per day
CLEANUP_INTERVAL_SECONDS = 86400
RETENTION_HOURS = 168  # 7 days

# Statuses that indicate a server should be health-checked
_CHECKABLE_STATUSES = {"active", "in_use", "ready"}


def _get_http_client() -> httpx.AsyncClient:
    """Create an HTTP client for metrics fetching."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _get_checkable_servers(servers: list) -> list:
    """Filter servers to only those that should be health-checked."""
    return [s for s in servers if s.is_managed and s.status in _CHECKABLE_STATUSES]


async def _fetch_metrics(http: httpx.AsyncClient, ip: str, port: int) -> str | None:
    """Fetch /metrics from a server, return text or None on failure."""
    try:
        resp = await http.get(f"http://{ip}:{port}/metrics")
        resp.raise_for_status()
        return resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


async def _check_server(server) -> None:
    """Run health check for a single server."""
    log = logger.bind(server_handle=server.handle, server_ip=server.public_ip)
    http = _get_http_client()

    try:
        # Fetch node_exporter metrics
        node_text = await _fetch_metrics(http, server.public_ip, NODE_EXPORTER_PORT)

        if node_text is None:
            # Server unreachable
            log.warning("server_unreachable", reason="node_exporter_fetch_failed")
            await _handle_unreachable(server)
            return

        # Fetch cadvisor metrics (non-critical — server can still be healthy)
        cadvisor_text = await _fetch_metrics(http, server.public_ip, CADVISOR_PORT)

        # Parse metrics
        node_metrics = parse_node_exporter(node_text)
        containers = parse_cadvisor(cadvisor_text) if cadvisor_text else []

        # Auto-resolve any active SERVER_UNREACHABLE incidents
        await _resolve_unreachable_incidents(server)

        # Convert to MB for used_ram/disk to match existing DB fields
        used_ram_mb = (
            int(node_metrics.ram_used_bytes / (1024 * 1024))
            if node_metrics.ram_used_bytes is not None
            else None
        )
        used_disk_mb = (
            int(node_metrics.disk_used_bytes / (1024 * 1024))
            if node_metrics.disk_used_bytes is not None
            else None
        )

        # Update server in DB
        update = ServerUpdate(
            cpu_usage_pct=round(node_metrics.cpu_usage_pct, 2)
            if node_metrics.cpu_usage_pct is not None
            else None,
            load_avg_1m=node_metrics.load_avg_1m,
            load_avg_5m=node_metrics.load_avg_5m,
            load_avg_15m=node_metrics.load_avg_15m,
            network_rx_errors=node_metrics.network_rx_errors,
            network_tx_errors=node_metrics.network_tx_errors,
            container_count_running=len(containers),
            container_count_total=len(containers),
            uptime_seconds=round(node_metrics.uptime_seconds, 0)
            if node_metrics.uptime_seconds is not None
            else None,
            used_ram_mb=used_ram_mb,
            used_disk_mb=used_disk_mb,
            last_health_check=datetime.now(UTC),
        )
        await api_client.update_server(server.handle, update)

        # Append metrics history
        history_metrics = {
            "cpu_usage_pct": node_metrics.cpu_usage_pct,
            "ram_used_bytes": node_metrics.ram_used_bytes,
            "ram_total_bytes": node_metrics.ram_total_bytes,
            "disk_used_bytes": node_metrics.disk_used_bytes,
            "disk_total_bytes": node_metrics.disk_total_bytes,
            "load_avg_1m": node_metrics.load_avg_1m,
            "load_avg_5m": node_metrics.load_avg_5m,
            "load_avg_15m": node_metrics.load_avg_15m,
            "uptime_seconds": node_metrics.uptime_seconds,
            "network_rx_errors": node_metrics.network_rx_errors,
            "network_tx_errors": node_metrics.network_tx_errors,
            "containers": [
                {
                    "name": c.name,
                    "cpu_usage_seconds": c.cpu_usage_seconds,
                    "memory_usage_bytes": c.memory_usage_bytes,
                    "memory_limit_bytes": c.memory_limit_bytes,
                }
                for c in containers
            ],
        }
        await api_client.create_metrics_history(server.handle, history_metrics)

        # Check resource thresholds
        await _check_resource_thresholds(server, node_metrics)

        log.debug(
            "health_check_ok",
            cpu_pct=node_metrics.cpu_usage_pct,
            ram_used_mb=used_ram_mb,
            containers=len(containers),
        )

    except Exception as e:
        log.error("health_check_error", error=str(e), error_type=type(e).__name__, exc_info=True)
    finally:
        await http.aclose()


async def _handle_unreachable(server) -> None:
    """Create SERVER_UNREACHABLE incident if none exists."""
    active = await api_client.get_active_incidents(server.handle, IncidentType.SERVER_UNREACHABLE)
    if active:
        return  # Already tracked

    await api_client.create_incident(
        server_handle=server.handle,
        incident_type=IncidentType.SERVER_UNREACHABLE,
        details={"reason": "node_exporter HTTP fetch failed", "ip": server.public_ip},
    )
    await notify_admins(
        f"Server *{server.handle}* ({server.public_ip}) is unreachable — "
        "node_exporter HTTP check failed.",
        level="critical",
    )


async def _resolve_unreachable_incidents(server) -> None:
    """Auto-resolve SERVER_UNREACHABLE incidents when server is back."""
    active = await api_client.get_active_incidents(server.handle, IncidentType.SERVER_UNREACHABLE)
    for incident in active:
        await api_client.resolve_incident(incident["id"])
        await notify_admins(
            f"Server *{server.handle}* ({server.public_ip}) is back online — "
            "incident auto-resolved.",
            level="success",
        )


async def _check_resource_thresholds(server, node_metrics) -> None:
    """Create RESOURCE_EXHAUSTED incidents for RAM/disk over threshold."""
    # RAM check
    if (
        node_metrics.ram_used_bytes is not None
        and node_metrics.ram_total_bytes
        and node_metrics.ram_total_bytes > 0
    ):
        ram_pct = (node_metrics.ram_used_bytes / node_metrics.ram_total_bytes) * 100
        if ram_pct > RAM_THRESHOLD_PCT:
            await _create_resource_incident(server, "ram", round(ram_pct, 1))

    # Disk check
    if (
        node_metrics.disk_used_bytes is not None
        and node_metrics.disk_total_bytes
        and node_metrics.disk_total_bytes > 0
    ):
        disk_pct = (node_metrics.disk_used_bytes / node_metrics.disk_total_bytes) * 100
        if disk_pct > DISK_THRESHOLD_PCT:
            await _create_resource_incident(server, "disk", round(disk_pct, 1))


async def _create_resource_incident(server, resource: str, usage_pct: float) -> None:
    """Create RESOURCE_EXHAUSTED incident if none exists."""
    active = await api_client.get_active_incidents(server.handle, IncidentType.RESOURCE_EXHAUSTED)
    if active:
        return  # Already tracked

    await api_client.create_incident(
        server_handle=server.handle,
        incident_type=IncidentType.RESOURCE_EXHAUSTED,
        details={
            "resource": resource,
            "usage_pct": usage_pct,
            "threshold_pct": RAM_THRESHOLD_PCT if resource == "ram" else DISK_THRESHOLD_PCT,
            "ip": server.public_ip,
        },
    )
    await notify_admins(
        f"Server *{server.handle}* ({server.public_ip}): "
        f"{resource.upper()} at {usage_pct}% (threshold: "
        f"{RAM_THRESHOLD_PCT if resource == 'ram' else DISK_THRESHOLD_PCT}%).",
        level="warning",
    )


async def _cleanup_old_history() -> int:
    """Delete metrics history older than retention period."""
    result = await api_client.delete_old_metrics_history(RETENTION_HOURS)
    deleted = result.get("deleted", 0)
    if deleted > 0:
        logger.info("metrics_history_cleanup", deleted=deleted, retention_hours=RETENTION_HOURS)

    # Also clean up application health history
    app_result = await api_client.delete_old_app_health_history(RETENTION_HOURS)
    app_deleted = app_result.get("deleted", 0)
    if app_deleted > 0:
        logger.info(
            "app_health_history_cleanup", deleted=app_deleted, retention_hours=RETENTION_HOURS
        )

    return deleted


async def health_check_worker():
    """Health checker worker — monitors server health via HTTP polling."""
    logger.info("health_check_worker_started", interval_sec=HEALTH_CHECK_INTERVAL)

    last_cleanup = time.monotonic()

    while True:
        start_time = time.time()
        checked = 0
        try:
            servers = await api_client.get_servers()
            checkable = _get_checkable_servers(servers)

            for server in checkable:
                await _check_server(server)
                checked += 1

            # Application health probing (after server checks)
            try:
                await app_health_probe_cycle()
            except Exception as e:
                logger.error(
                    "app_health_probe_error",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )

            # Daily cleanup
            now = time.monotonic()
            if now - last_cleanup > CLEANUP_INTERVAL_SECONDS:
                await _cleanup_old_history()
                last_cleanup = now

        except Exception as e:
            logger.error(
                "health_check_worker_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
        finally:
            duration = time.time() - start_time
            logger.info(
                "health_check_cycle_complete",
                servers_checked=checked,
                duration_sec=round(duration, 2),
            )

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
