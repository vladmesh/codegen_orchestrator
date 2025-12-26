"""Server sync worker - syncs servers and their specs from Time4VPS."""

import asyncio
import json
import os
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.time4vps import Time4VPSClient
from shared.models.api_key import APIKey
from shared.models.server import Server
from shared.notifications import notify_admins
from src.db import async_session_maker

from .provisioner_trigger import publish_provisioner_trigger

logger = structlog.get_logger()

# Config
GHOST_SERVERS = os.getenv("GHOST_SERVERS", "").split(",")
GHOST_SERVERS = [ip.strip() for ip in GHOST_SERVERS if ip.strip()]

# How often to sync (seconds)
SYNC_INTERVAL = 60
# How often to fetch detailed specs (more expensive, do less often)
DETAILS_SYNC_INTERVAL = 300  # 5 minutes


async def get_time4vps_client(db: AsyncSession) -> Time4VPSClient | None:
    """Create Time4VPS client with credentials from DB."""
    query = select(APIKey).where(APIKey.service == "time4vps")
    result = await db.execute(query)
    api_key = result.scalar_one_or_none()

    if not api_key:
        return None

    try:
        creds = json.loads(api_key.key_enc)
        return Time4VPSClient(creds["username"], creds["password"])
    except (json.JSONDecodeError, KeyError) as e:
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
            async with async_session_maker() as db:
                client = await get_time4vps_client(db)
                if not client:
                    logger.warning("time4vps_credentials_missing")
                else:
                    # Basic sync every iteration
                    (
                        servers_discovered,
                        servers_updated,
                        servers_missing,
                    ) = await _sync_server_list(db, client)

                    # Detailed specs sync less frequently
                    now = asyncio.get_event_loop().time()
                    if now - last_details_sync > DETAILS_SYNC_INTERVAL:
                        details_updated = await _sync_server_details(db, client)
                        last_details_sync = now

                    # Check for servers requiring provisioning
                    triggers_published = await _check_provisioning_triggers(db)

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


async def _sync_server_list(db: AsyncSession, client: Time4VPSClient) -> tuple[int, int, int]:
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

    # Fetch existing servers from DB
    result = await db.execute(select(Server))
    db_servers = {s.public_ip: s for s in result.scalars().all()}

    # Track new managed servers for notification
    new_managed_servers = []
    discovered_count = 0
    updated_count = 0
    missing_count = 0

    for srv in api_servers:
        ip = srv.get("ip")
        if not ip:
            continue

        server_id = srv.get("server_id")
        if not server_id:
            logger.warning(f"Server with IP {ip} has no server_id, skipping")
            continue

        hostname = srv.get("domain")
        is_ghost = ip in GHOST_SERVERS

        existing = db_servers.get(ip)

        if existing:
            # Server exists - update if was missing
            if existing.status == "missing":
                existing.status = "active"
                updated_count += 1
                logger.info("server_reappeared", server_ip=ip)
            # Update provider_id if changed
            if existing.labels.get("provider_id") != str(server_id):
                existing.labels = {**existing.labels, "provider_id": str(server_id)}
                updated_count += 1
        else:
            # New Server Discovered
            # Check if it's a ghost server or managed
            is_managed = not is_ghost

            # Managed servers need provisioning by default
            if is_managed:
                status = "pending_setup"
                logger.info(
                    "managed_server_discovered",
                    server_ip=ip,
                    server_handle=f"vps-{server_id}",
                    status=status,
                )
            else:
                status = "reserved"  # Ghost servers are reserved
                logger.info(
                    "ghost_server_discovered",
                    server_ip=ip,
                    server_handle=f"vps-{server_id}",
                )

            new_server = Server(
                handle=f"vps-{server_id}",
                host=hostname,
                public_ip=ip,
                is_managed=is_managed,
                status=status,
                labels={"provider_id": str(server_id)},
            )
            db.add(new_server)
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
    api_ips = {s.get("ip") for s in api_servers if s.get("ip")}
    for ip, srv in db_servers.items():
        if ip not in api_ips and srv.status != "missing":
            srv.status = "missing"
            missing_count += 1
            logger.warning("server_missing_from_time4vps", server_ip=ip)

    await db.commit()

    # Send notifications for new managed servers
    for server in new_managed_servers:
        await notify_admins(
            f"New managed server discovered: *{server.handle}* ({server.public_ip}). "
            "Provisioning will be triggered automatically.",
            level="info",
        )
    return discovered_count, updated_count, missing_count


async def _sync_server_details(db: AsyncSession, client: Time4VPSClient) -> int:
    """Fetch detailed specs for each server (RAM, disk, OS)."""
    logger.info("server_details_sync_start")

    result = await db.execute(select(Server).where(Server.status.notin_(["missing"])))
    servers = result.scalars().all()

    updated_count = 0

    for server in servers:
        provider_id = server.labels.get("provider_id")
        if not provider_id:
            continue

        try:
            details = await client.get_server_details(int(provider_id))

            # Update capacity and usage from API
            server.capacity_cpu = details.get("cpu_cores", server.capacity_cpu)
            server.capacity_ram_mb = details.get("ram_limit", server.capacity_ram_mb)
            server.capacity_disk_mb = details.get("disk_limit", server.capacity_disk_mb)
            server.used_ram_mb = details.get("ram_used", 0)
            server.used_disk_mb = details.get("disk_usage", 0)
            server.os_template = details.get("os")

            # Update status based on Time4VPS status
            api_status = details.get("status", "").lower()
            if api_status == "active" and server.status == "discovered":
                server.status = "active"

            updated_count += 1
            logger.debug(
                "server_details_updated",
                server_handle=server.handle,
                ram_mb=server.capacity_ram_mb,
                disk_mb=server.capacity_disk_mb,
            )

        except Exception as e:
            logger.warning(
                "server_details_fetch_failed",
                server_handle=server.handle,
                error=str(e),
                error_type=type(e).__name__,
            )
            continue

    await db.commit()
    logger.info("server_details_sync_complete", updated_count=updated_count)
    return updated_count


async def _check_provisioning_triggers(db: AsyncSession) -> int:
    """Check for servers that need provisioning.

    Looks for:
    - PENDING_SETUP servers (new managed servers)
    - FORCE_REBUILD servers (manual trigger)

    Automatically triggers provisioning via Redis pub/sub.
    """
    triggers_published = 0
    # Check for FORCE_REBUILD
    result = await db.execute(select(Server).where(Server.status == "force_rebuild"))
    force_rebuild_servers = result.scalars().all()

    for server in force_rebuild_servers:
        logger.warning(
            "server_force_rebuild_trigger",
            server_handle=server.handle,
        )

        # Update status to PROVISIONING before triggering
        server.status = "provisioning"
        await db.commit()

        # Trigger provisioner
        await publish_provisioner_trigger(server.handle, is_incident_recovery=False)
        triggers_published += 1

        # Notify admins
        await notify_admins(
            f"Force rebuild triggered for server *{server.handle}*. Provisioning started.",
            level="warning",
        )

    # Check for PENDING_SETUP
    result = await db.execute(select(Server).where(Server.status == "pending_setup"))
    pending_servers = result.scalars().all()

    for server in pending_servers:
        logger.info("server_pending_setup_trigger", server_handle=server.handle)

        # Update status to PROVISIONING before triggering
        server.status = "provisioning"
        await db.commit()

        # Trigger provisioner
        await publish_provisioner_trigger(server.handle, is_incident_recovery=False)
        triggers_published += 1
    return triggers_published
