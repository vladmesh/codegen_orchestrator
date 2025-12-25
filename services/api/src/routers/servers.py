"""Servers router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# TODO: Add encryption logic
# from cryptography.fernet import Fernet
# ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
from ..database import get_async_session
from ..models import PortAllocation, Server, ServiceDeployment
from ..schemas import (
    PortAllocationCreate,
    PortAllocationRead,
    ServerCreate,
    ServerRead,
    ServiceDeploymentRead,
)

router = APIRouter(prefix="/servers", tags=["servers"])


@router.post("/", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_in: ServerCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Server:
    """Create a new server."""
    if await db.get(Server, server_in.handle):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server with this handle already exists",
        )

    # TODO: Encrypt ssh_key
    ssh_key_encrypted = server_in.ssh_key  # Mock encryption

    server = Server(
        handle=server_in.handle,
        host=server_in.host,
        public_ip=server_in.public_ip,
        ssh_user=server_in.ssh_user,
        ssh_key_enc=ssh_key_encrypted,
        capacity_cpu=server_in.capacity_cpu,
        capacity_ram_mb=server_in.capacity_ram_mb,
        labels=server_in.labels,
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
) -> list[Server]:
    """List all servers with optional filtering."""
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
) -> Server:
    """Get a server by handle."""
    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.get("/{handle}/ports", response_model=list[PortAllocationRead])
async def list_server_ports(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
) -> list[PortAllocation]:
    """List all port allocations for a server."""
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
) -> PortAllocation:
    """Allocate a port on a server."""
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


@router.patch("/{handle}", response_model=ServerRead)
async def update_server(
    handle: str,
    updates: dict,
    db: AsyncSession = Depends(get_async_session),
) -> Server:
    """Update server fields (e.g., status for FORCE_REBUILD)."""
    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Update allowed fields
    allowed_fields = {"status", "notes", "is_managed", "labels"}
    for field, value in updates.items():
        if field in allowed_fields and hasattr(server, field):
            setattr(server, field, value)

    await db.commit()
    await db.refresh(server)
    return server


@router.post("/{handle}/force-rebuild", response_model=ServerRead)
async def force_rebuild_server(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
) -> Server:
    """Trigger FORCE_REBUILD for a server."""
    from ..models import ServerStatus

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
) -> list:
    """Get incident history for a server."""
    from ..models import Incident

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
) -> dict:
    """Manual provisioning trigger (will integrate with LangGraph)."""
    from ..models import ServerStatus

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


@router.get("/{handle}/services", response_model=list[ServiceDeploymentRead])
async def get_server_services(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
) -> list[ServiceDeployment]:
    """Get all services deployed on a specific server."""
    # Verify server exists
    if not await db.get(Server, handle):
        raise HTTPException(status_code=404, detail="Server not found")

    query = (
        select(ServiceDeployment)
        .where(
            ServiceDeployment.server_handle == handle,
            ServiceDeployment.status == "running",  # Only active deployments
        )
        .order_by(ServiceDeployment.deployed_at.desc())
    )

    result = await db.execute(query)
    return result.scalars().all()
