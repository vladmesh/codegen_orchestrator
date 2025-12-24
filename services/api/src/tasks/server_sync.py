import asyncio
import os
import logging
import json
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from ..database import async_session_maker
from ..models.server import Server
from ..models.api_key import APIKey
from ..clients.time4vps import Time4VPSClient

logger = logging.getLogger(__name__)

# Config
GHOST_SERVERS = os.getenv("GHOST_SERVERS", "").split(",")
GHOST_SERVERS = [ip.strip() for ip in GHOST_SERVERS if ip.strip()]

async def get_time4vps_creds(db: AsyncSession) -> dict | None:
    """Fetch Time4VPS credentials from DB."""
    query = select(APIKey).where(APIKey.service == "time4vps")
    result = await db.execute(query)
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        return None
        
    # TODO: Implement decryption here when centralized. Currently mocked.
    try:
        val = json.loads(api_key.key_enc)
        return val
    except json.JSONDecodeError:
        logger.error("Failed to decode Time4VPS credentials")
        return None

async def sync_servers_worker():
    """Background worker to sync servers from Time4VPS."""
    logger.info("Starting Server Sync Worker")
    
    while True:
        try:
            async with async_session_maker() as db:
                await _sync_servers(db)
        except Exception as e:
            logger.error(f"Error in Server Sync Worker: {e}", exc_info=True)
        
        # Sync every 60 seconds
        await asyncio.sleep(60)

async def _sync_servers(db: AsyncSession):
    creds = await get_time4vps_creds(db)
    if not creds:
        logger.warning("Time4VPS credentials not found. Skipping sync.")
        return

    client = Time4VPSClient(creds["username"], creds["password"])
    
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
            
        server_id = srv.get("server_id")  # Time4VPS uses 'server_id' not 'id'
        if not server_id:
            logger.warning(f"Server with IP {ip} has no server_id, skipping")
            continue
            
        hostname = srv.get("domain") # hostname/domain
        
        # Check if ignored (Ghost)
        is_ghost = ip in GHOST_SERVERS
        
        existing = db_servers.get(ip)
        
        if existing:
            # Update
            if existing.status == "missing":
                existing.status = "active"
                logger.info(f"Server {ip} reappeared")
            
            # Update detailed status/specs here if needed
            # For now we assume active if present in API listing
        else:
            # New Server Discovered
            status = "discovered" if not is_ghost else "reserved"
            is_managed = not is_ghost
            
            # We assume CPU/RAM from API logic if available, currently defaulting
            # Ideally fetch details: details = await client.get_server_details(server_id)
            # But let's avoid n+1 calls for now, just register basic info
            
            new_server = Server(
                handle=f"vps-{server_id}", # Generate handle from Time4VPS ID
                host=hostname,
                public_ip=ip,
                is_managed=is_managed,
                status=status,
                capacity_cpu=1, # Default
                capacity_ram_mb=1024, # Default
                labels={"provider_id": str(server_id)}
            )
            db.add(new_server)
            logger.info(f"Discovered new server: {ip} (Ghost: {is_ghost})")
            
    # Check for missing servers
    api_ips = {s.get("ip") for s in api_servers if s.get("ip")}
    for ip, srv in db_servers.items():
        if ip not in api_ips:
            if srv.status != "missing":
                srv.status = "missing"
                logger.warning(f"Server {ip} is missing from Time4VPS API!")
    
    await db.commit()
