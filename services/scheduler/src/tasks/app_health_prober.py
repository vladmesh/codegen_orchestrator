"""Application health prober — checks deployed apps via HTTP + SSL.

Probes each deployed application's health endpoint, tracks response times,
consecutive failures (→ SERVICE_DOWN incidents), and SSL cert expiry
(→ SSL_EXPIRING incidents). Stores history for uptime calculation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.clients.infra_client import check_http_health
from shared.contracts.dto.application import ApplicationStatus
from shared.models.incident import IncidentType
from shared.notifications import notify_admins
from src.clients.api import api_client
from src.tasks.ssl_checker import check_ssl_expiry

from .. import startup

logger = structlog.get_logger()


def _consecutive_failure_threshold() -> int:
    return startup.get_config().get_int("health.consecutive_failure_threshold")


def _ssl_expiry_warning_days() -> int:
    return startup.get_config().get_int("health.ssl_expiry_warning_days")


# In-memory state for consecutive failure tracking (reset on worker restart)
_consecutive_failures: dict[int, int] = {}


async def check_application(
    app,
    server_ip: str,
    consecutive_failures: int,
    api_client: object,
) -> int:
    """Check a single application's health.

    Returns updated consecutive failure count.
    """
    app_id = app.id
    ports = app.ports
    if not ports:
        return consecutive_failures

    # Use the first port for health check
    port = ports[0]["port"]
    log = logger.bind(app_id=app_id, service=app.service_name, server_ip=server_ip, port=port)

    # HTTP health check
    url = f"http://{server_ip}:{port}/health"
    health = await check_http_health(url)
    healthy = health.get("healthy", False)

    # SSL expiry check
    ssl_expiry = await check_ssl_expiry(server_ip, port)

    now = datetime.now(UTC)

    if healthy:
        # Update application as running
        fields = {
            "status": ApplicationStatus.RUNNING.value,
            "response_time_ms": health.get("response_time_ms"),
            "last_health_check": now.isoformat(),
        }
        if ssl_expiry:
            fields["ssl_expires_at"] = ssl_expiry.isoformat()

        await api_client.update_application(app_id, fields)

        # Auto-resolve SERVICE_DOWN incidents on recovery
        if consecutive_failures > 0:
            active = await api_client.get_active_incidents(
                app.server_handle, IncidentType.SERVICE_DOWN
            )
            for incident in active:
                await api_client.resolve_incident(incident.id)
                await notify_admins(
                    f"Application *{app.service_name}* on {server_ip} is back — "
                    "SERVICE_DOWN incident resolved.",
                    level="success",
                )

        log.debug("app_health_ok", response_time_ms=health.get("response_time_ms"))
        consecutive_failures = 0
    else:
        consecutive_failures += 1

        # Update application status to DOWN
        fields = {
            "status": ApplicationStatus.DOWN.value,
            "last_health_check": now.isoformat(),
        }
        await api_client.update_application(app_id, fields)

        # Create SERVICE_DOWN incident after threshold
        if consecutive_failures >= _consecutive_failure_threshold():
            active = await api_client.get_active_incidents(
                app.server_handle, IncidentType.SERVICE_DOWN
            )
            if not active:
                await api_client.create_incident(
                    server_handle=app.server_handle,
                    incident_type=IncidentType.SERVICE_DOWN,
                    details={
                        "application_id": app_id,
                        "service_name": app.service_name,
                        "consecutive_failures": consecutive_failures,
                        "last_error": health.get("error", "unknown"),
                    },
                    affected_services=[app.service_name],
                )
                await notify_admins(
                    f"Application *{app.service_name}* on {server_ip} is DOWN — "
                    f"{consecutive_failures} consecutive failures.",
                    level="critical",
                )

        log.warning(
            "app_health_failed",
            consecutive_failures=consecutive_failures,
            error=health.get("error"),
        )

    # SSL expiry incident check
    if ssl_expiry:
        days_until_expiry = (ssl_expiry - now).days
        if days_until_expiry < _ssl_expiry_warning_days():
            active = await api_client.get_active_incidents(
                app.server_handle, IncidentType.SSL_EXPIRING
            )
            if not active:
                await api_client.create_incident(
                    server_handle=app.server_handle,
                    incident_type=IncidentType.SSL_EXPIRING,
                    details={
                        "application_id": app_id,
                        "service_name": app.service_name,
                        "ssl_expires_at": ssl_expiry.isoformat(),
                        "days_until_expiry": days_until_expiry,
                    },
                    affected_services=[app.service_name],
                )
                await notify_admins(
                    f"SSL cert for *{app.service_name}* on {server_ip} "
                    f"expires in {days_until_expiry} days.",
                    level="warning",
                )

    # Append health history
    await api_client.create_app_health_history(
        app_id,
        {
            "healthy": healthy,
            "response_time_ms": health.get("response_time_ms"),
            "status_code": health.get("status_code"),
            "ssl_expires_at": ssl_expiry.isoformat() if ssl_expiry else None,
        },
    )

    return consecutive_failures


async def app_health_probe_cycle(client: object | None = None) -> None:
    """Run one full cycle of application health probing.

    Fetches all deployed applications, groups by server, probes each.
    """
    client = client or api_client

    # Get all applications (exclude not_deployed)
    apps = await client.get_applications()
    deployed_apps = [a for a in apps if a.status != ApplicationStatus.NOT_DEPLOYED.value]

    if not deployed_apps:
        return

    # Build server IP lookup
    servers = await client.get_servers()
    server_ips = {s.handle: s.public_ip for s in servers}

    for app in deployed_apps:
        app_id = app.id
        server_handle = app.server_handle
        server_ip = server_ips.get(server_handle)

        if not server_ip:
            logger.warning("app_prober_no_server_ip", app_id=app_id, server_handle=server_handle)
            continue

        ports = app.ports
        if not ports:
            logger.debug("app_prober_no_ports", app_id=app_id, service=app.service_name)
            continue

        prev_failures = _consecutive_failures.get(app_id, 0)
        try:
            new_failures = await check_application(
                app=app,
                server_ip=server_ip,
                consecutive_failures=prev_failures,
                api_client=client,
            )
            _consecutive_failures[app_id] = new_failures
        except Exception:
            logger.error(
                "app_health_check_error",
                app_id=app_id,
                service=app.service_name,
                exc_info=True,
            )

    # Compute uptime_pct_24h for each probed app
    for app in deployed_apps:
        app_id = app.id
        try:
            await _update_uptime(app_id, client)
        except Exception:
            logger.debug("uptime_calc_error", app_id=app_id, exc_info=True)


async def _update_uptime(app_id: int, client: object) -> None:
    """Calculate and update 24h uptime percentage from health history."""
    # The API returns history for last N hours
    # We need to fetch and compute: healthy_count / total_count * 100
    try:
        resp = await client._request(
            "GET", f"applications/{app_id}/health-history", params={"hours": 24}
        )
        history = resp.json()
    except Exception:
        return

    if not history:
        return

    total = len(history)
    healthy_count = sum(1 for h in history if h.get("metrics", {}).get("healthy"))
    uptime_pct = round((healthy_count / total) * 100, 2)

    await client.update_application(app_id, {"uptime_pct_24h": uptime_pct})
