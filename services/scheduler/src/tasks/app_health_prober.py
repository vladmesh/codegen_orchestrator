"""Application health prober — checks deployed apps via HTTP + SSL.

Probes each deployed application's health endpoint, tracks response times,
consecutive failures (→ SERVICE_DOWN incidents), and SSL cert expiry
(→ SSL_EXPIRING incidents). Stores history for uptime calculation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from shared.clients.infra_client import check_http_health
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.incident import IncidentType
from shared.notifications import notify_admins_best_effort
from src.clients.api import api_client
from src.tasks.ssl_checker import check_ssl_expiry

from .. import startup

logger = structlog.get_logger()


def _consecutive_failure_threshold() -> int:
    return startup.get_config().get_int("health.consecutive_failure_threshold")


def _ssl_expiry_warning_days() -> int:
    return startup.get_config().get_int("health.ssl_expiry_warning_days")


# In-memory state for consecutive failure tracking (reset on worker restart).
# An application missing from it has no failure history in this worker: it was
# never probed here, or its monitoring was switched since the count was taken.
_consecutive_failures: dict[int, int] = {}
# The monitoring generation (`monitoring_changed_at`) each count belongs to. A
# count taken before an off/on switch is stale even if the off state was never
# observed by a cycle.
_monitoring_generation: dict[int, datetime | None] = {}


class MonitoringDisabledError(Exception):
    """The API refused a prober incident write: the application is muted now."""


def _refused_as_muted(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code == httpx.codes.CONFLICT


def monitoring_generation(app) -> str:
    """The switch generation a probe starts from; the API compares it under its lock."""
    changed_at = app.monitoring_changed_at
    return "initial" if changed_at is None else changed_at.isoformat()


async def _create_incident_if_monitored(api_client: object, app, **kwargs) -> None:
    """Create an incident unless the switch moved since the probe began.

    The API decides under the application's row lock. A notification already
    accepted before a later switch may still be delivered: muting stops new
    incident transitions, it does not recall alerts sent before it.
    """
    try:
        await api_client.create_incident(**kwargs, monitoring_generation=monitoring_generation(app))
    except httpx.HTTPStatusError as exc:
        if _refused_as_muted(exc):
            raise MonitoringDisabledError from exc
        raise


async def _resolve_incident_if_monitored(api_client: object, app, incident_id: int) -> None:
    try:
        await api_client.resolve_incident(
            incident_id, monitoring_generation=monitoring_generation(app)
        )
    except httpx.HTTPStatusError as exc:
        if _refused_as_muted(exc):
            raise MonitoringDisabledError from exc
        raise


async def _app_service_down_incidents(app, api_client: object) -> list:
    """Active SERVICE_DOWN incidents of this application, not of its whole server.

    Several applications share a server; an incident is this application's only
    when its details name it. A legacy incident without an application id belongs
    to nobody here, so it neither suppresses nor is closed by any application.
    """
    active = await api_client.list_active_incidents()  # detected + recovering
    return [
        i
        for i in active
        if i.incident_type is IncidentType.SERVICE_DOWN
        and i.server_handle == app.server_handle
        and i.details.get("application_id") == app.id
    ]


async def check_application(
    app,
    server_ip: str,
    consecutive_failures: int,
    api_client: object,
    *,
    has_history: bool = True,
) -> int:
    """Check a single application's health.

    *has_history* is False when this worker holds no failure count for the
    application. Returns updated consecutive failure count. Raises
    MonitoringDisabledError when the API refused an incident write because the
    application's monitoring was switched off while it was being probed; no alert
    is sent for a refused write.
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

        # Auto-resolve this application's SERVICE_DOWN incidents on recovery. With
        # no failure history in this worker (a restart, or monitoring re-enabled)
        # an incident may still be open from before, so look it up too.
        if consecutive_failures > 0 or not has_history:
            for incident in await _app_service_down_incidents(app, api_client):
                await _resolve_incident_if_monitored(api_client, app, incident.id)
                await notify_admins_best_effort(
                    f"Application *{app.service_name}* on {server_ip} is back — "
                    "SERVICE_DOWN incident resolved.",
                    level="success",
                    component="app_health_prober",
                    application_id=app_id,
                    server_handle=app.server_handle,
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
            if not await _app_service_down_incidents(app, api_client):
                await _create_incident_if_monitored(
                    api_client,
                    app,
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
                await notify_admins_best_effort(
                    f"Application *{app.service_name}* on {server_ip} is DOWN — "
                    f"{consecutive_failures} consecutive failures.",
                    level="critical",
                    component="app_health_prober",
                    application_id=app_id,
                    server_handle=app.server_handle,
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
                await _create_incident_if_monitored(
                    api_client,
                    app,
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
                await notify_admins_best_effort(
                    f"SSL cert for *{app.service_name}* on {server_ip} "
                    f"expires in {days_until_expiry} days.",
                    level="warning",
                    component="app_health_prober",
                    application_id=app_id,
                    server_handle=app.server_handle,
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

    # Get all applications (exclude not_deployed and those with monitoring disabled)
    apps = await client.get_applications()
    for app in apps:
        if not app.monitoring_enabled or (
            app.id in _monitoring_generation
            and _monitoring_generation[app.id] != app.monitoring_changed_at
        ):
            # Forget the failure count: after a switch, health transitions are
            # decided from fresh probes, not from a count gathered before it.
            _consecutive_failures.pop(app.id, None)
        _monitoring_generation[app.id] = app.monitoring_changed_at
    deployed_apps = [
        a for a in apps if a.status != ApplicationStatus.NOT_DEPLOYED.value and a.monitoring_enabled
    ]

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
                has_history=app_id in _consecutive_failures,
            )
            _consecutive_failures[app_id] = new_failures
        except MonitoringDisabledError:
            # Switched off mid-probe: no alert went out, and this probe's count
            # must not carry over into a later re-enable.
            _consecutive_failures.pop(app_id, None)
            logger.info("app_health_monitoring_disabled_mid_probe", app_id=app_id)
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
        resp = await client.request(
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
