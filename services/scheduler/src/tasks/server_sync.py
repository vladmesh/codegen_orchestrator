"""Server sync worker - syncs servers and their specs from Time4VPS."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import time
from typing import NamedTuple

import structlog

from shared.clients.time4vps import Time4VPSAPIError, Time4VPSClient
from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import ServerCreate, ServerDTO, ServerStatus, ServerUpdate
from shared.notifications import notify_admins_best_effort
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    managed_provider_ids,
    normalize_provider_id,
    provider_operation_is_authorized,
)
from src.clients.api import api_client

from .. import startup
from .provisioner_trigger import publish_provisioner_trigger

logger = structlog.get_logger()

# Label for the external dependency this worker depends on, stored in incident details.
PROVIDER_DEPENDENCY = "time4vps_api"
_SCHEDULED_STATUSES = {
    ServerStatus.PENDING_SETUP,
    ServerStatus.PROVISIONING,
    ServerStatus.FORCE_REBUILD,
}


class ManagementChange(NamedTuple):
    """One allowlist-driven management transition."""

    handle: str
    provider_id: int
    was_managed: bool
    is_managed: bool


class TriggerRule(NamedTuple):
    """Status-specific scheduling behavior for one provisioning publication."""

    wait: timedelta
    transition: ServerUpdate
    log_event: str
    warning: bool = False
    notification: str | None = None


def _sync_interval() -> int:
    return startup.get_config().get_int("scheduler.server_sync_interval")


def _details_sync_interval() -> int:
    return startup.get_config().get_int("scheduler.server_details_sync_interval")


def _provisioning_stuck_timeout() -> int:
    return startup.get_config().get_int("scheduler.provisioning_stuck_timeout_seconds")


def _provisioning_cooldown() -> int:
    return startup.get_config().get_int("scheduler.provisioning_trigger_cooldown_seconds")


async def get_time4vps_client() -> Time4VPSClient | None:
    """Create Time4VPS client with credentials from DB."""
    api_key_data = await api_client.get_api_key("time4vps")

    if not api_key_data or "value" not in api_key_data:
        return None

    try:
        # API returns decrypted value in "value" field
        creds = api_key_data["value"]
        if isinstance(creds, str):
            creds = json.loads(creds)

        return Time4VPSClient(creds["username"], creds["password"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Failed to parse Time4VPS credentials: {e}")
        return None


async def sync_servers_worker():
    """Background worker to sync servers from Time4VPS."""
    logger.info("server_sync_worker_started")

    last_details_sync = 0

    while True:
        start_time = time.time()
        servers_discovered = 0
        servers_updated = 0
        servers_missing = 0
        details_updated = 0
        triggers_published = 0
        incidents_resolved = 0
        sync_completed = False
        failure_reason: str | None = None
        try:
            client = await get_time4vps_client()
            if not client:
                failure_reason = "time4vps_credentials_missing"
                logger.warning("time4vps_credentials_missing")
            else:
                # Basic sync every iteration
                (
                    servers_discovered,
                    servers_updated,
                    servers_missing,
                ) = await _sync_server_list(client)

                # Detailed specs sync less frequently
                now = time.monotonic()
                if now - last_details_sync > _details_sync_interval():
                    details_updated = await _sync_server_details(client)
                    last_details_sync = now

                # Check for servers requiring provisioning
                triggers_published = await _check_provisioning_triggers()
                incidents_resolved = await _reconcile_provisioning_incidents()
                sync_completed = True

        except Exception as e:
            failure_reason = f"{type(e).__name__}: {e}"
            logger.error(
                "server_sync_worker_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
        finally:
            duration = time.time() - start_time
            counters = {
                "servers_discovered": servers_discovered,
                "servers_updated": servers_updated,
                "servers_missing": servers_missing,
                "details_updated": details_updated,
                "triggers_published": triggers_published,
                "incidents_resolved": incidents_resolved,
                "duration_sec": round(duration, 2),
            }
            if sync_completed:
                logger.info("server_sync_complete", **counters)
            else:
                # Zero counters from an aborted cycle read exactly like "no servers
                # exist". Never report an unfinished cycle at info level.
                logger.error("server_sync_incomplete", reason=failure_reason, **counters)

        await asyncio.sleep(_sync_interval())


async def _reconcile_existing_server(
    existing: ServerDTO,
    *,
    server_id: int,
    ip: str,
    hostname: str,
    should_manage: bool,
) -> tuple[bool, ManagementChange | None]:
    """Persist identity/policy drift without scheduling an existing server."""
    update = ServerUpdate()
    # This is the explicit legacy upgrade path. It only attaches Time4VPS
    # identity after this producer has matched the existing stable provider ID;
    # it never infers a provider from an IP address.
    if existing.provider != TIME4VPS_PROVIDER:
        update.provider = TIME4VPS_PROVIDER
    if normalize_provider_id(TIME4VPS_PROVIDER, existing.provider_id) != str(server_id):
        update.provider_id = str(server_id)
    if existing.public_ip != ip:
        update.public_ip = ip
    if existing.host != hostname:
        update.host = hostname
    if existing.status == ServerStatus.UNREACHABLE:
        update.status = ServerStatus.ACTIVE if should_manage else ServerStatus.RESERVED
        logger.info("server_reappeared", server_handle=existing.handle, server_ip=ip)

    management_change = None
    if existing.is_managed != should_manage:
        update.is_managed = should_manage
        management_change = ManagementChange(
            handle=existing.handle,
            provider_id=server_id,
            was_managed=existing.is_managed,
            is_managed=should_manage,
        )

    # Promotion grants eligibility but must never preserve stale scheduled work. Demoted
    # scheduled rows are neutralized by the canonical trigger-policy boundary below.
    if management_change is not None and should_manage and existing.status in _SCHEDULED_STATUSES:
        update.status = ServerStatus.RESERVED

    if not update.model_fields_set:
        return False, management_change
    await api_client.update_server(existing.handle, update)
    return True, management_change


async def _transition_and_publish(server: ServerDTO, transition: ServerUpdate) -> bool:
    """Publish after a state transition and neutralize work denied by the final guard."""
    await api_client.update_server(server.handle, transition)
    if await publish_provisioner_trigger(server.handle, is_incident_recovery=False):
        return True

    await api_client.update_server(
        server.handle,
        ServerUpdate(status=ServerStatus.RESERVED, provisioning_started_at=None),
    )
    logger.warning(
        "provisioning_transition_neutralized",
        server_handle=server.handle,
        previous_status=server.status,
        reason="authorization_changed_before_publish",
    )
    await notify_admins_best_effort(
        f"Provisioning for *{server.handle}* was cancelled because its authorization changed "
        "before queue publication. The server was moved to reserved.",
        level="warning",
        component="server_sync",
        server_handle=server.handle,
    )
    return False


async def _report_management_changes(
    changes: list[ManagementChange], previous_by_handle: dict[str, ServerDTO]
) -> None:
    """Neutralize scheduled demotions and report allowlist-driven changes."""
    for change in changes:
        previous = previous_by_handle[change.handle]
        if change.was_managed and not change.is_managed and previous.status in _SCHEDULED_STATUSES:
            await _neutralize_unauthorized_trigger(previous)
        logger.warning(
            "server_management_changed",
            server_handle=change.handle,
            provider_id=change.provider_id,
            was_managed=change.was_managed,
            is_managed=change.is_managed,
            reason="provider_id_allowlist",
        )
        await notify_admins_best_effort(
            f"Server management changed for *{change.handle}* "
            f"(provider ID {change.provider_id}): managed={change.was_managed} "
            f"→ managed={change.is_managed}. "
            "Existing servers are never auto-provisioned by this change.",
            level="warning",
            component="server_sync",
            server_handle=change.handle,
        )

    demoted = [change for change in changes if change.was_managed and not change.is_managed]
    if len(demoted) > 1:
        await notify_admins_best_effort(
            f"Critical provisioning policy change: {len(demoted)} servers were demoted in one "
            "sync cycle. Check the Time4VPS provider policy before continuing operations.",
            level="critical",
            component="server_sync",
        )


async def _sync_server_list(client: Time4VPSClient) -> tuple[int, int, int]:  # noqa: PLR0915
    """Sync basic server list - discover new, mark missing.

    Raises whatever the provider call raised: a cycle that could not read the
    provider has not synced anything and must not report zero counters as a result.
    """
    try:
        api_servers = await client.get_servers()
    except Exception as e:
        logger.error(
            "time4vps_server_fetch_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await _record_provider_outage(e)
        raise

    await _resolve_provider_outage()

    # An absent or empty provider policy intentionally means no provider server is managed.
    managed_server_ids = managed_provider_ids(TIME4VPS_PROVIDER)

    # Provider ID is the stable identity. A legacy row can be upgraded only by
    # this Time4VPS producer and only after an exact stable-ID match.
    db_servers_list = await api_client.get_servers()
    db_servers_by_handle = {server.handle: server for server in db_servers_list}
    db_servers_by_provider_id = {}
    for server in db_servers_list:
        if server.provider not in (None, TIME4VPS_PROVIDER):
            continue
        provider_id = normalize_provider_id(TIME4VPS_PROVIDER, server.provider_id)
        if provider_id is not None:
            db_servers_by_provider_id[int(provider_id)] = server

    new_managed_servers = []
    management_changes: list[ManagementChange] = []
    discovered_count = 0
    updated_count = 0
    missing_count = 0

    for srv in api_servers:
        ip = srv.ip
        if not ip:
            continue

        server_id = srv.id
        if not server_id:
            logger.warning(f"Server with IP {ip} has no server_id, skipping")
            continue

        hostname = srv.domain or ip
        should_manage = str(server_id) in managed_server_ids

        existing = db_servers_by_provider_id.get(server_id)

        if existing:
            was_updated, management_change = await _reconcile_existing_server(
                existing,
                server_id=server_id,
                ip=ip,
                hostname=hostname,
                should_manage=should_manage,
            )
            if was_updated:
                updated_count += 1
            if management_change:
                management_changes.append(management_change)
        else:
            # New Server Discovered
            is_managed = should_manage

            # Managed servers need provisioning by default
            if is_managed:
                status = ServerStatus.PENDING_SETUP
                logger.info(
                    "managed_server_discovered",
                    server_ip=ip,
                    server_handle=f"vps-{server_id}",
                    status=status,
                )
            else:
                status = ServerStatus.RESERVED
                logger.info(
                    "unmanaged_server_discovered",
                    server_ip=ip,
                    server_handle=f"vps-{server_id}",
                )

            server_create = ServerCreate(
                handle=f"vps-{server_id}",
                host=hostname,
                public_ip=ip,
                is_managed=is_managed,
                status=status,
                labels={"provider": TIME4VPS_PROVIDER, "provider_id": str(server_id)},
            )
            new_server = await api_client.create_server(server_create)

            logger.info(
                "server_discovered",
                server_ip=ip,
                server_handle=f"vps-{server_id}",
                is_managed=is_managed,
            )
            discovered_count += 1

            # Track new managed servers for notification
            if is_managed:
                new_managed_servers.append(new_server)

    api_server_ids = {str(server.id) for server in api_servers}
    for server in db_servers_list:
        if server.provider != TIME4VPS_PROVIDER:
            continue
        provider_id = normalize_provider_id(TIME4VPS_PROVIDER, server.provider_id)
        if provider_id is None:
            logger.warning(
                "time4vps_server_identity_invalid",
                server_handle=server.handle,
                provider_id=server.provider_id,
            )
            continue
        is_missing = provider_id not in api_server_ids
        if is_missing and server.status != ServerStatus.UNREACHABLE:
            await api_client.update_server(
                server.handle, ServerUpdate(status=ServerStatus.UNREACHABLE)
            )
            missing_count += 1
            logger.warning(
                "server_missing_from_time4vps",
                server_handle=server.handle,
                server_ip=server.public_ip,
            )

    await _report_management_changes(management_changes, db_servers_by_handle)

    # Send notifications for new managed servers
    for server in new_managed_servers:
        await notify_admins_best_effort(
            f"New managed server discovered: *{server.handle}* ({server.public_ip}). "
            "Provisioning will be triggered automatically.",
            level="info",
            component="server_sync",
            server_handle=server.handle,
        )
    return discovered_count, updated_count, missing_count


def _outage_details(error: Exception) -> dict:
    """Describe a provider failure, keeping the response body when there is one."""
    details: dict = {
        "dependency": PROVIDER_DEPENDENCY,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    if isinstance(error, Time4VPSAPIError):
        details["status_code"] = error.status_code
        details["response_body"] = error.body
    return details


async def _active_provider_outages() -> list:
    incidents = await api_client.list_active_incidents()
    return [
        incident
        for incident in incidents
        if incident.incident_type is IncidentType.PROVIDER_API_UNAVAILABLE
    ]


async def _record_provider_outage(error: Exception) -> None:
    """Open one incident per provider outage, not one signal per failed cycle."""
    if await _active_provider_outages():
        return  # Already tracked: the outage is one incident, not one per minute

    details = _outage_details(error)
    await api_client.create_incident(
        server_handle=None,
        incident_type=IncidentType.PROVIDER_API_UNAVAILABLE,
        details=details,
    )
    await notify_admins_best_effort(
        f"Time4VPS API is unavailable, server sync is stopped. Reason: {details['error']}",
        level="critical",
        component="server_sync",
    )


async def _resolve_provider_outage() -> None:
    """Close the outage incident once the provider answers again."""
    for incident in await _active_provider_outages():
        await api_client.resolve_incident(incident.id)
        await notify_admins_best_effort(
            "Time4VPS API is reachable again, server sync resumed. Incident auto-resolved.",
            level="success",
            component="server_sync",
        )


async def _sync_server_details(client: Time4VPSClient) -> int:
    """Fetch detailed specs for each server (RAM, disk, OS)."""
    logger.info("server_details_sync_start")

    servers = await api_client.get_servers()
    servers = [s for s in servers if s.status != ServerStatus.UNREACHABLE]

    updated_count = 0

    for server in servers:
        if server.provider != TIME4VPS_PROVIDER:
            continue
        provider_id = normalize_provider_id(TIME4VPS_PROVIDER, server.provider_id)
        if provider_id is None:
            continue

        try:
            details_model = await client.get_server_details(int(provider_id))
            details = details_model.model_dump()

            # Prepare update
            update_data = ServerUpdate(
                capacity_cpu=details.get("cpu_cores", server.capacity_cpu),
                capacity_ram_mb=details.get("ram_limit", server.capacity_ram_mb),
                capacity_disk_mb=details.get("disk_limit", server.capacity_disk_mb),
                os_template=details.get("os"),
            )
            if server.last_health_check is None:
                # The hypervisor counts page cache as used, so once the health
                # checker reports real MemAvailable-based usage, the provider's
                # inflated numbers must not overwrite it — the allocator reads
                # used_ram_mb and would starve on a healthy server.
                update_data.used_ram_mb = details.get("ram_used", 0)
                update_data.used_disk_mb = details.get("disk_usage", 0)

            # Check if status update is needed
            api_status = details.get("status", "").lower()
            if api_status == "active" and server.status == ServerStatus.NEW:
                update_data.status = ServerStatus.ACTIVE

            await api_client.update_server(server.handle, update_data)
            updated_count += 1

            logger.debug(
                "server_details_updated",
                server_handle=server.handle,
                ram_mb=update_data.capacity_ram_mb,
                disk_mb=update_data.capacity_disk_mb,
            )

        except Exception as e:
            logger.warning(
                "server_details_fetch_failed",
                server_handle=server.handle,
                error=str(e),
                error_type=type(e).__name__,
            )
            continue

    logger.info("server_details_sync_complete", updated_count=updated_count)
    return updated_count


async def _neutralize_unauthorized_trigger(server: ServerDTO) -> None:
    """Cancel stale scheduled work that no longer passes provisioning policy."""
    await api_client.update_server(
        server.handle,
        ServerUpdate(status=ServerStatus.RESERVED, provisioning_started_at=None),
    )
    logger.warning(
        "provisioning_trigger_neutralized",
        server_handle=server.handle,
        previous_status=server.status,
        reason="server_not_authorized",
    )
    await notify_admins_best_effort(
        f"Provisioning for *{server.handle}* was cancelled because the server is no longer "
        "authorized. It was moved to reserved.",
        level="warning",
        component="server_sync",
        server_handle=server.handle,
    )


async def _check_provisioning_triggers() -> int:
    """Check for servers that need provisioning.

    Looks for:
    - PENDING_SETUP servers (new managed servers)
    - FORCE_REBUILD servers (manual trigger)

    Automatically triggers provisioning via Redis pub/sub.
    """
    triggers_published = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    stuck_timeout = timedelta(seconds=_provisioning_stuck_timeout())
    trigger_cooldown = timedelta(seconds=_provisioning_cooldown())

    # Check for servers needing action
    # We fetch all servers and filter in memory.
    # In a larger system, API should support filtering by status list.
    all_servers = await api_client.get_servers()
    actionable: list[ServerDTO] = []
    for server in all_servers:
        if server.status not in _SCHEDULED_STATUSES:
            continue
        if provider_operation_is_authorized(
            provider=server.provider,
            provider_id=server.provider_id,
            is_managed=server.is_managed,
        ):
            actionable.append(server)
        else:
            await _neutralize_unauthorized_trigger(server)

    rules = {
        ServerStatus.FORCE_REBUILD: TriggerRule(
            wait=stuck_timeout,
            # Keep FORCE_REBUILD until infra-service consumes the explicit reinstall intent.
            transition=ServerUpdate(provisioning_started_at=now),
            log_event="server_force_rebuild_trigger",
            warning=True,
            notification="Force rebuild triggered",
        ),
        ServerStatus.PENDING_SETUP: TriggerRule(
            wait=trigger_cooldown,
            transition=ServerUpdate(
                status=ServerStatus.PROVISIONING,
                provisioning_started_at=now,
            ),
            log_event="server_pending_setup_trigger",
        ),
        ServerStatus.PROVISIONING: TriggerRule(
            wait=stuck_timeout,
            transition=ServerUpdate(provisioning_started_at=now),
            log_event="provisioning_timeout_trigger",
            warning=True,
        ),
    }

    for server in actionable:
        rule = rules[server.status]
        if server.provisioning_started_at is None:
            if server.status == ServerStatus.PROVISIONING:
                # A legacy/incomplete transition has no publication timestamp. Mark it now
                # and wait one full stuck window rather than publishing an immediate duplicate.
                await api_client.update_server(
                    server.handle, ServerUpdate(provisioning_started_at=now)
                )
                logger.info("provisioning_start_marked", server_handle=server.handle)
                continue
        elif now - server.provisioning_started_at < rule.wait:
            logger.info(
                "provisioning_trigger_wait_skipped",
                server_handle=server.handle,
                status=server.status,
                started_at=server.provisioning_started_at.isoformat(),
                wait_seconds=int(rule.wait.total_seconds()),
            )
            continue

        log = logger.warning if rule.warning else logger.info
        log(
            rule.log_event,
            server_handle=server.handle,
            attempts=server.provisioning_attempts,
        )
        if await _transition_and_publish(server, rule.transition):
            triggers_published += 1
            if rule.notification:
                await notify_admins_best_effort(
                    f"{rule.notification} for server *{server.handle}*. Provisioning started.",
                    level="warning",
                    component="server_sync",
                    server_handle=server.handle,
                )

    return triggers_published


async def _reconcile_provisioning_incidents() -> int:
    """Close stale provisioning-failure journal entries for READY servers.

    This only reconciles the journal. It never triggers provisioning or recovery.
    A failed update is logged without notification because provisioning success has
    already emitted one actionable warning; leaving the incident active retries it
    on the next tick without a notification storm.
    """
    servers = await api_client.get_servers()
    ready_handles = {server.handle for server in servers if server.status == ServerStatus.READY}
    active_incidents = await api_client.list_active_incidents()
    resolved_count = 0

    for incident in active_incidents:
        if (
            incident.incident_type is not IncidentType.PROVISIONING_FAILED
            or incident.server_handle not in ready_handles
        ):
            continue
        try:
            await api_client.resolve_incident(incident.id)
        except Exception as exc:
            logger.error(
                "provisioning_incident_reconciliation_failed",
                incident_id=incident.id,
                server_handle=incident.server_handle,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            continue
        resolved_count += 1
        logger.info(
            "provisioning_incident_reconciled",
            incident_id=incident.id,
            server_handle=incident.server_handle,
        )

    return resolved_count
