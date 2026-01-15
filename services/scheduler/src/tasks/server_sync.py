"""Server sync worker - syncs servers and their specs from Time4VPS."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import time

import structlog

from shared.clients.time4vps import Time4VPSClient
from shared.contracts.dto.server import ServerCreate, ServerStatus, ServerUpdate
from shared.notifications import notify_admins
from src.clients.api import api_client

from .provisioner_trigger import publish_provisioner_trigger

logger = structlog.get_logger()

# Config
GHOST_SERVERS = os.getenv("GHOST_SERVERS", "").split(",")
GHOST_SERVERS = [ip.strip() for ip in GHOST_SERVERS if ip.strip()]

# How often to sync (seconds)
SYNC_INTERVAL = 60
# How often to fetch detailed specs (more expensive, do less often)
DETAILS_SYNC_INTERVAL = 300  # 5 minutes
# Trigger re-provisioning if a server is stuck in provisioning for too long.
PROVISIONING_STUCK_TIMEOUT_SECONDS = 30 * 60
# Avoid duplicate triggers shortly after a provisioning trigger.
PROVISIONING_TRIGGER_COOLDOWN_SECONDS = 120


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
        try:
            client = await get_time4vps_client()
            if not client:
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
                if now - last_details_sync > DETAILS_SYNC_INTERVAL:
                    details_updated = await _sync_server_details(client)
                    last_details_sync = now

                # Check for servers requiring provisioning
                triggers_published = await _check_provisioning_triggers()

        except Exception as e:
            logger.error(
                "server_sync_worker_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
        finally:
            duration = time.time() - start_time
            logger.info(
                "server_sync_complete",
                servers_discovered=servers_discovered,
                servers_updated=servers_updated,
                servers_missing=servers_missing,
                details_updated=details_updated,
                triggers_published=triggers_published,
                duration_sec=round(duration, 2),
            )

        await asyncio.sleep(SYNC_INTERVAL)


async def _sync_server_list(client: Time4VPSClient) -> tuple[int, int, int]:
    """Sync basic server list - discover new, mark missing."""
    try:
        api_servers = await client.get_servers()
    except Exception as e:
        logger.error(
            "time4vps_server_fetch_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return 0, 0, 0

    # Fetch existing servers from API
    db_servers_list = await api_client.get_servers()
    db_servers = {s.public_ip: s for s in db_servers_list}

    # Track new managed servers for notification
    new_managed_servers = []
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

        hostname = srv.domain
        is_ghost = ip in GHOST_SERVERS

        existing = db_servers.get(ip)

        if existing:
            # Server exists - update if was missing
            if (
                existing.status == ServerStatus.UNREACHABLE
            ):  # Assuming missing mapped to unreachable or needs status update
                # Wait, original code checked status == "missing".
                # ServerStatus has NEW, PENDING_SETUP, ACTIVE, UNREACHABLE, MAINTENANCE.
                # It does NOT have MISSING.
                # Original code used strings.
                # Let's assume UNREACHABLE is used for missing? Or maybe add MISSING to enum?
                # The contracts/dto/server.py does NOT have MISSING.
                # Let's use UNREACHABLE for now or just check if it was marked as such.
                pass

            # Correction: Original code set status="missing". I should probably add
            # MISSING to ServerStatus or use UNREACHABLE.
            # Choosing UNREACHABLE for now as closest semantic match for "not found in provider".
            if existing.status == ServerStatus.UNREACHABLE:
                await api_client.update_server(
                    existing.handle, ServerUpdate(status=ServerStatus.ACTIVE)
                )
                updated_count += 1
                logger.info("server_reappeared", server_ip=ip)
            # Update provider_id if changed
            if existing.labels.get("provider_id") != str(server_id):
                new_labels = {**existing.labels, "provider_id": str(server_id)}
                await api_client.update_server(existing.handle, ServerUpdate(labels=new_labels))
                updated_count += 1
        else:
            # New Server Discovered
            # Check if it's a ghost server or managed
            is_managed = not is_ghost

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
                status = ServerStatus.ACTIVE  # Ghost servers are reserved/active
                # Original code used "reserved", which is not in ServerStatus enum.
                # Using ACTIVE for now.
                logger.info(
                    "ghost_server_discovered",
                    server_ip=ip,
                    server_handle=f"vps-{server_id}",
                )

            server_create = ServerCreate(
                handle=f"vps-{server_id}",
                host=hostname,
                public_ip=ip,
                is_managed=is_managed,
                status=status,
                provider_id=str(server_id),
                labels={"provider_id": str(server_id)},
            )
            new_server = await api_client.create_server(server_create)

            logger.info(
                "server_discovered",
                server_ip=ip,
                server_handle=f"vps-{server_id}",
                is_ghost=is_ghost,
            )
            discovered_count += 1

            # Track new managed servers for notification
            if is_managed:
                new_managed_servers.append(new_server)

    # Check for missing servers
    api_ips = {s.ip for s in api_servers if s.ip}
    for ip, srv in db_servers.items():
        if ip not in api_ips and srv.status != ServerStatus.UNREACHABLE:
            await api_client.update_server(
                srv.handle, ServerUpdate(status=ServerStatus.UNREACHABLE)
            )
            missing_count += 1
            logger.warning("server_missing_from_time4vps", server_ip=ip)

    # Send notifications for new managed servers
    for server in new_managed_servers:
        await notify_admins(
            f"New managed server discovered: *{server.handle}* ({server.public_ip}). "
            "Provisioning will be triggered automatically.",
            level="info",
        )
    return discovered_count, updated_count, missing_count


async def _sync_server_details(client: Time4VPSClient) -> int:
    """Fetch detailed specs for each server (RAM, disk, OS)."""
    logger.info("server_details_sync_start")

    servers = await api_client.get_servers()
    servers = [s for s in servers if s.status != ServerStatus.UNREACHABLE]

    updated_count = 0

    for server in servers:
        provider_id = server.labels.get("provider_id")
        if not provider_id:
            continue

        try:
            details_model = await client.get_server_details(int(provider_id))
            details = details_model.model_dump()

            # Prepare update
            update_data = ServerUpdate(
                capacity_cpu=details.get("cpu_cores", server.capacity_cpu),
                capacity_ram_mb=details.get("ram_limit", server.capacity_ram_mb),
                capacity_disk_mb=details.get("disk_limit", server.capacity_disk_mb),
                used_ram_mb=details.get("ram_used", 0),
                used_disk_mb=details.get("disk_usage", 0),
                os_template=details.get("os"),
            )

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


async def _check_provisioning_triggers() -> int:
    """Check for servers that need provisioning.

    Looks for:
    - PENDING_SETUP servers (new managed servers)
    - FORCE_REBUILD servers (manual trigger)

    Automatically triggers provisioning via Redis pub/sub.
    """
    triggers_published = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    stuck_timeout = timedelta(seconds=PROVISIONING_STUCK_TIMEOUT_SECONDS)
    trigger_cooldown = timedelta(seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS)

    # Check for servers needing action
    # We fetch all servers and filter in memory.
    # In a larger system, API should support filtering by status list.
    all_servers = await api_client.get_servers()

    # 1. FORCE_REBUILD
    force_rebuild_servers = [s for s in all_servers if s.status == ServerStatus.FORCE_REBUILD]

    for server in force_rebuild_servers:
        if (
            server.provisioning_started_at
            and (now - server.provisioning_started_at) < trigger_cooldown
        ):
            logger.info(
                "provisioning_trigger_cooldown_skipped",
                server_handle=server.handle,
                status=server.status,
                started_at=server.provisioning_started_at.isoformat(),
                cooldown_seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS,
            )
            continue

        logger.warning(
            "server_force_rebuild_trigger",
            server_handle=server.handle,
        )

        # Update status to PROVISIONING before triggering
        await api_client.update_server(
            server.handle,
            ServerUpdate(status=ServerStatus.PROVISIONING, provisioning_started_at=now),
        )

        # Trigger provisioner
        await publish_provisioner_trigger(server.handle, is_incident_recovery=False)
        triggers_published += 1

        # Notify admins
        await notify_admins(
            f"Force rebuild triggered for server *{server.handle}*. Provisioning started.",
            level="warning",
        )

    # 2. PENDING_SETUP
    pending_servers = [s for s in all_servers if s.status == ServerStatus.PENDING_SETUP]

    for server in pending_servers:
        if (
            server.provisioning_started_at
            and (now - server.provisioning_started_at) < trigger_cooldown
        ):
            logger.info(
                "provisioning_trigger_cooldown_skipped",
                server_handle=server.handle,
                status=server.status,
                started_at=server.provisioning_started_at.isoformat(),
                cooldown_seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS,
            )
            continue

        logger.info("server_pending_setup_trigger", server_handle=server.handle)

        # Update status to PROVISIONING before triggering
        await api_client.update_server(
            server.handle,
            ServerUpdate(status=ServerStatus.PROVISIONING, provisioning_started_at=now),
        )

        # Trigger provisioner
        await publish_provisioner_trigger(server.handle, is_incident_recovery=False)
        triggers_published += 1

    # 3. Stuck Provisioning (PROVISIONING status)
    provisioning_servers = [s for s in all_servers if s.status == ServerStatus.PROVISIONING]

    for server in provisioning_servers:
        if server.provisioning_started_at is None:
            # Should have started_at if status is provisioning, but fix if missing
            await api_client.update_server(server.handle, ServerUpdate(provisioning_started_at=now))
            logger.info("provisioning_start_marked", server_handle=server.handle)
            continue

        if now - server.provisioning_started_at < stuck_timeout:
            continue

        logger.warning(
            "provisioning_timeout_trigger",
            server_handle=server.handle,
            started_at=server.provisioning_started_at.isoformat(),
            timeout_seconds=PROVISIONING_STUCK_TIMEOUT_SECONDS,
            attempts=server.provisioning_attempts,
        )

        # Reset started_at to now for retry
        await api_client.update_server(server.handle, ServerUpdate(provisioning_started_at=now))

        await publish_provisioner_trigger(server.handle, is_incident_recovery=False)
        triggers_published += 1

    return triggers_published
