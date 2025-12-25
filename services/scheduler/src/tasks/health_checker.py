"""Health checker worker - monitors server health via SSH."""

import asyncio
from datetime import datetime
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.notifications import notify_admins
from src.db import async_session_maker
from src.models.incident import Incident, IncidentStatus
from src.models.server import Server

from .provisioner_trigger import publish_provisioner_trigger

logger = logging.getLogger(__name__)

# Configuration
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # 1 minute
SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "10"))  # 10 seconds
SSH_KEY_PATH = os.getenv("ORCHESTRATOR_SSH_PRIVATE_KEY_PATH", "/root/.ssh/id_ed25519")


async def _check_server_health(server: Server) -> bool:
    """Check if server is reachable via SSH.

    Args:
        server: Server to check

    Returns:
        True if healthy (SSH accessible), False otherwise
    """
    if not server.public_ip:
        logger.warning(f"Server {server.handle} has no public IP, skipping health check")
        return True  # Assume healthy if no IP to check

    try:
        # Use asyncio subprocess to test SSH connectivity
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-i",
            SSH_KEY_PATH,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={SSH_TIMEOUT}",
            "-o",
            "BatchMode=yes",
            f"root@{server.public_ip}",
            "echo ok",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for SSH with timeout
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=SSH_TIMEOUT + 5,  # Extra buffer
        )

        is_healthy = process.returncode == 0 and b"ok" in stdout

        if not is_healthy:
            logger.warning(
                f"SSH health check failed for {server.handle} ({server.public_ip}): "
                f"returncode={process.returncode}, stderr={stderr.decode()[:200]}"
            )

        return is_healthy

    except TimeoutError:
        logger.warning(f"SSH timeout for server {server.handle} ({server.public_ip})")
        return False
    except FileNotFoundError:
        logger.error(f"SSH key not found at {SSH_KEY_PATH}")
        return True  # Don't mark as unhealthy if SSH key missing
    except Exception as e:
        logger.warning(f"SSH check failed for {server.handle}: {e}")
        return False


async def _handle_unhealthy_server(db: AsyncSession, server: Server):
    """Handle detected unhealthy server.

    1. Check for active incidents (avoid duplicates)
    2. Create new incident if needed
    3. Update server status to ERROR
    4. Trigger recovery via Provisioner
    5. Send notification to admins

    Args:
        db: Database session
        server: Unhealthy server
    """
    # Check if there's already an active incident for this server
    result = await db.execute(
        select(Incident).where(
            Incident.server_handle == server.handle,
            Incident.status.in_([IncidentStatus.DETECTED.value, IncidentStatus.RECOVERING.value]),
        )
    )
    active_incident = result.scalar_one_or_none()

    if active_incident:
        # Already handling this incident, just update recovery attempts
        logger.info(
            f"Active incident #{active_incident.id} exists for "
            f"{server.handle}, incrementing attempts"
        )
        active_incident.recovery_attempts += 1
        return

    # Create new incident
    incident = Incident(
        server_handle=server.handle,
        incident_type="server_unreachable",
        status=IncidentStatus.DETECTED.value,
        details={
            "detection_method": "ssh_health_check",
            "last_known_status": server.status,
            "detected_at": datetime.utcnow().isoformat(),
        },
        affected_services=[],  # TODO: Get services from port allocations
    )
    db.add(incident)
    await db.flush()  # Flush to get incident.id

    # Update server status
    previous_status = server.status
    server.status = "error"
    server.last_incident = datetime.utcnow()

    logger.error(
        f"🚨 Server {server.handle} is UNHEALTHY! "
        f"Created incident #{incident.id}. "
        f"Previous status: {previous_status}"
    )

    # Trigger recovery via Provisioner
    await publish_provisioner_trigger(server.handle, is_incident_recovery=True)
    logger.info(f"🔄 Triggered automatic recovery for {server.handle}")

    # Send notification to admins
    await notify_admins(
        f"Server *{server.handle}* ({server.public_ip}) is unreachable! "
        f"Incident #{incident.id} created. Automatic recovery initiated.",
        level="critical",
    )


async def _check_all_servers(db: AsyncSession):
    """Check health of all managed servers in ready/in_use status.

    Args:
        db: Database session
    """
    # Get all servers that should be monitored
    result = await db.execute(
        select(Server).where(Server.is_managed, Server.status.in_(["ready", "in_use"]))
    )
    servers = result.scalars().all()

    if not servers:
        logger.debug("No servers to health check")
        return

    logger.info(f"Running health checks for {len(servers)} servers...")

    healthy_count = 0
    unhealthy_count = 0

    for server in servers:
        is_healthy = await _check_server_health(server)

        if is_healthy:
            # Update last_health_check timestamp
            server.last_health_check = datetime.utcnow()
            healthy_count += 1
            logger.debug(f"✓ {server.handle} is healthy")
        else:
            # Handle unhealthy server
            await _handle_unhealthy_server(db, server)
            unhealthy_count += 1

    await db.commit()

    logger.info(f"Health check complete: {healthy_count} healthy, {unhealthy_count} unhealthy")


async def health_check_worker():
    """Background worker to check server health continuously.

    Runs every HEALTH_CHECK_INTERVAL seconds.
    """
    logger.info(f"Starting Health Check Worker (interval: {HEALTH_CHECK_INTERVAL}s)")

    while True:
        try:
            async with async_session_maker() as db:
                await _check_all_servers(db)
        except Exception as e:
            logger.error(f"Error in Health Check Worker: {e}", exc_info=True)

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
