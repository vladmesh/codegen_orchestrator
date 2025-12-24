"""Server sync worker - syncs servers and their specs from Time4VPS."""

import asyncio
import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clients.time4vps import Time4VPSClient
from ..database import async_session_maker
from ..models.api_key import APIKey
from ..models.server import Server

logger = logging.getLogger(__name__)

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
    logger.info("Starting Server Sync Worker")

    last_details_sync = 0

    while True:
        try:
            async with async_session_maker() as db:
                client = await get_time4vps_client(db)
                if not client:
                    logger.warning("Time4VPS credentials not found. Skipping sync.")
                else:
                    # Basic sync every iteration
                    await _sync_server_list(db, client)

                    # Detailed specs sync less frequently
                    now = asyncio.get_event_loop().time()
                    if now - last_details_sync > DETAILS_SYNC_INTERVAL:
                        await _sync_server_details(db, client)
                        last_details_sync = now

        except Exception as e:
            logger.error(f"Error in Server Sync Worker: {e}", exc_info=True)

        await asyncio.sleep(SYNC_INTERVAL)


async def _sync_server_list(db: AsyncSession, client: Time4VPSClient):
    """Sync basic server list - discover new, mark missing."""
    try:
        api_servers = await client.get_servers()
    except Exception as e:
        logger.error(f"Failed to fetch servers from Time4VPS: {e}")
        return

    # Fetch existing servers from DB
    result = await db.execute(select(Server))
    db_servers = {s.public_ip: s for s in result.scalars().all()}

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
                logger.info(f"Server {ip} reappeared")
            # Update provider_id if changed
            if existing.labels.get("provider_id") != str(server_id):
                existing.labels = {**existing.labels, "provider_id": str(server_id)}
        else:
            # New Server Discovered
            status = "discovered" if not is_ghost else "reserved"
            is_managed = not is_ghost

            new_server = Server(
                handle=f"vps-{server_id}",
                host=hostname,
                public_ip=ip,
                is_managed=is_managed,
                status=status,
                labels={"provider_id": str(server_id)},
            )
            db.add(new_server)
            logger.info(f"Discovered new server: {ip} (handle: vps-{server_id}, ghost: {is_ghost})")

    # Check for missing servers
    api_ips = {s.get("ip") for s in api_servers if s.get("ip")}
    for ip, srv in db_servers.items():
        if ip not in api_ips and srv.status != "missing":
            srv.status = "missing"
            logger.warning(f"Server {ip} is missing from Time4VPS API!")

    await db.commit()


async def _sync_server_details(db: AsyncSession, client: Time4VPSClient):
    """Fetch detailed specs for each server (RAM, disk, OS)."""
    logger.info("Syncing server details from Time4VPS...")

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
                f"Updated specs for {server.handle}: "
                f"RAM {server.capacity_ram_mb}MB, Disk {server.capacity_disk_mb}MB"
            )

        except Exception as e:
            logger.warning(f"Failed to fetch details for server {server.handle}: {e}")
            continue

    await db.commit()
    logger.info(f"Updated details for {updated_count} servers")
