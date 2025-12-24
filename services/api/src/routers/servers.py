"""Servers router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# TODO: Add encryption logic
# from cryptography.fernet import Fernet
# ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

from ..database import get_async_session
from ..models import Server, PortAllocation
from ..schemas import ServerCreate, ServerRead, PortAllocationCreate, PortAllocationRead

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
    ssh_key_encrypted = server_in.ssh_key # Mock encryption

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
    db: AsyncSession = Depends(get_async_session)
) -> list[Server]:
    """List all servers with optional filtering."""
    query = select(Server)
    
    if is_managed is not None:
        query = query.where(Server.is_managed == is_managed)
    
    if status is not None:
        query = query.where(Server.status == status)
        
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
        PortAllocation.server_handle == handle,
        PortAllocation.port == allocation_in.port
    )
    if (await db.execute(query)).scalar_one_or_none():
         raise HTTPException(status_code=400, detail=f"Port {allocation_in.port} is already allocated on this server")

    allocation = PortAllocation(
        server_handle=handle,
        port=allocation_in.port,
        service_name=allocation_in.service_name,
        project_id=allocation_in.project_id
    )
    db.add(allocation)
    await db.commit()
    await db.refresh(allocation)
    return allocation
