"""Servers router."""

from datetime import UTC

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.server import ServerStatus
from shared.crypto import SecretsCipher
from shared.models import Application, PortAllocation, Server, User

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas import (
    AllocateNextPortRequest,
    ApplicationRead,
    MetricsHistoryCreate,
    MetricsHistoryRead,
    PortAllocationCreate,
    PortAllocationRead,
    ServerCreate,
    ServerRead,
)

router = APIRouter(prefix="/servers", tags=["servers"])


@router.post("/", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_in: ServerCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Create a new server (admin only)."""
    if await db.get(Server, server_in.handle):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server with this handle already exists",
        )

    ssh_key_encrypted = SecretsCipher().encrypt(server_in.ssh_key) if server_in.ssh_key else None

    server = Server(
        handle=server_in.handle,
        host=server_in.host,
        public_ip=server_in.public_ip,
        ssh_user=server_in.ssh_user,
        ssh_key_enc=ssh_key_encrypted,
        capacity_cpu=server_in.capacity_cpu,
        capacity_ram_mb=server_in.capacity_ram_mb,
        labels=server_in.labels,
        status=server_in.status,
        is_managed=server_in.is_managed,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return server


@router.get("/", response_model=list[ServerRead])
async def list_servers(
    is_managed: bool | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list[Server]:
    """List all servers with optional filtering (admin only)."""
    query = select(Server)

    if is_managed is not None:
        query = query.where(Server.is_managed == is_managed)

    if status is not None:
        query = query.where(Server.status == status)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{handle}", response_model=ServerRead)
async def get_server(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Get a server by handle (admin only)."""
    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.get("/{handle}/ssh-key")
async def get_server_ssh_key(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Get decrypted SSH private key for a server (admin/internal only)."""
    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    if not server.ssh_key_enc:
        raise HTTPException(status_code=404, detail="No SSH key stored for this server")

    decrypted = SecretsCipher().decrypt(server.ssh_key_enc)
    return {"ssh_key": decrypted}


@router.get("/{handle}/ports", response_model=list[PortAllocationRead])
async def list_server_ports(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list[PortAllocation]:
    """List all port allocations for a server (admin only)."""
    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    query = select(PortAllocation).where(PortAllocation.server_handle == handle)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/{handle}/ports", response_model=PortAllocationRead)
async def allocate_port(
    handle: str,
    allocation_in: PortAllocationCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PortAllocation:
    """Allocate a port on a server (admin only)."""
    # Check server exists
    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    # Check if port is free
    query = select(PortAllocation).where(
        PortAllocation.server_handle == handle, PortAllocation.port == allocation_in.port
    )
    if (await db.execute(query)).scalar_one_or_none():
        raise HTTPException(
            status_code=400, detail=f"Port {allocation_in.port} is already allocated on this server"
        )

    allocation = PortAllocation(
        server_handle=handle,
        port=allocation_in.port,
        service_name=allocation_in.service_name,
        project_id=allocation_in.project_id,
    )
    db.add(allocation)
    await db.commit()
    await db.refresh(allocation)
    return allocation


@router.post("/{handle}/ports/allocate-next", response_model=PortAllocationRead)
async def allocate_next_port(
    handle: str,
    req: AllocateNextPortRequest,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PortAllocation:
    """Atomically find and allocate the next available port.

    Uses SELECT FOR UPDATE to prevent race conditions between concurrent
    allocation requests. Retries with the next port if a conflict occurs.
    """
    from sqlalchemy.exc import IntegrityError

    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    max_retries = 10
    for _attempt in range(max_retries):
        # Get all allocated ports with row-level lock
        query = (
            select(PortAllocation.port)
            .where(PortAllocation.server_handle == handle)
            .with_for_update()
        )
        result = await db.execute(query)
        allocated_ports = {row[0] for row in result.all()}

        # Find next available
        port = req.start_port
        while port in allocated_ports:
            port += 1

        allocation = PortAllocation(
            server_handle=handle,
            port=port,
            service_name=req.service_name,
            application_id=req.application_id,
        )
        db.add(allocation)
        try:
            await db.commit()
            await db.refresh(allocation)
            return allocation
        except IntegrityError:
            await db.rollback()
            continue

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Failed to allocate port after max retries",
    )


@router.patch("/{handle}", response_model=ServerRead)
async def update_server(
    handle: str,
    updates: dict,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Update server fields (admin only)."""
    from datetime import datetime

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Handle ssh_key specially — encrypt before storing
    if "ssh_key" in updates:
        raw_key = updates.pop("ssh_key")
        if raw_key:
            server.ssh_key_enc = SecretsCipher().encrypt(raw_key)
        else:
            server.ssh_key_enc = None

    # Update allowed fields
    allowed_fields = {
        "status",
        "notes",
        "is_managed",
        "labels",
        "provisioning_started_at",
        "capacity_cpu",
        "capacity_ram_mb",
        "capacity_disk_mb",
        "used_ram_mb",
        "used_disk_mb",
        "os_template",
        # Health metrics
        "cpu_usage_pct",
        "load_avg_1m",
        "load_avg_5m",
        "load_avg_15m",
        "network_rx_errors",
        "network_tx_errors",
        "container_count_running",
        "container_count_total",
        "uptime_seconds",
        "last_health_check",
    }
    # Fields that need datetime parsing
    datetime_fields = {"provisioning_started_at", "last_health_check"}

    for field, value in updates.items():
        if field in allowed_fields and hasattr(server, field):
            # Parse datetime strings
            if field in datetime_fields and isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                # Strip tz for tz-naive DB columns
                if value.tzinfo is not None:
                    value = value.replace(tzinfo=None)
            setattr(server, field, value)

    await db.commit()
    await db.refresh(server)
    return server


@router.post("/{handle}/force-rebuild", response_model=ServerRead)
async def force_rebuild_server(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Trigger FORCE_REBUILD for a server (admin only)."""

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    server.status = ServerStatus.FORCE_REBUILD.value
    await db.commit()
    await db.refresh(server)
    return server


@router.get("/{handle}/incidents")
async def get_server_incidents(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list:
    """Get incident history for a server (admin only)."""
    from shared.models import Incident

    # Verify server exists
    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    query = (
        select(Incident)
        .where(Incident.server_handle == handle)
        .order_by(Incident.detected_at.desc())
    )

    result = await db.execute(query)
    incidents = result.scalars().all()

    return [
        {
            "id": inc.id,
            "incident_type": inc.incident_type,
            "status": inc.status,
            "detected_at": inc.detected_at,
            "resolved_at": inc.resolved_at,
            "details": inc.details,
            "affected_services": inc.affected_services,
            "recovery_attempts": inc.recovery_attempts,
        }
        for inc in incidents
    ]


@router.post("/{handle}/provision")
async def provision_server(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Manual provisioning trigger (admin only)."""

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Set status to provisioning
    server.status = ServerStatus.PROVISIONING.value
    await db.commit()

    # TODO: Trigger LangGraph provisioner node via queue/webhook
    # For now, just return success
    return {
        "message": f"Provisioning triggered for server {handle}",
        "server_handle": handle,
        "status": server.status,
    }


@router.get("/{handle}/applications", response_model=list[ApplicationRead])
async def get_server_applications(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list[Application]:
    """Get all applications on a specific server (admin only)."""
    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    query = (
        select(Application)
        .where(
            Application.server_handle == handle,
            Application.status != ApplicationStatus.STOPPED.value,
        )
        .order_by(Application.service_name)
    )

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{handle}/metrics-history", response_model=list[MetricsHistoryRead])
async def get_metrics_history(
    handle: str,
    hours: int = 24,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list:
    """Get metrics history for a server (admin only)."""
    from datetime import datetime, timedelta

    from shared.models import ServerMetricsHistory

    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(ServerMetricsHistory)
        .where(
            ServerMetricsHistory.server_handle == handle,
            ServerMetricsHistory.recorded_at >= cutoff,
        )
        .order_by(ServerMetricsHistory.recorded_at.desc())
    )

    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/metrics-history")
async def delete_old_metrics_history(
    retention_hours: int = 168,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Delete metrics history older than retention_hours (default 7 days)."""
    from datetime import datetime, timedelta

    from sqlalchemy import delete as sa_delete

    from shared.models import ServerMetricsHistory

    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    stmt = sa_delete(ServerMetricsHistory).where(ServerMetricsHistory.recorded_at < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount}


@router.post(
    "/{handle}/metrics-history",
    response_model=MetricsHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_metrics_history(
    handle: str,
    snapshot: MetricsHistoryCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> object:
    """Append a metrics history snapshot for a server (internal use)."""
    from shared.models import ServerMetricsHistory

    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    entry = ServerMetricsHistory(
        server_handle=handle,
        metrics=snapshot.metrics,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry
