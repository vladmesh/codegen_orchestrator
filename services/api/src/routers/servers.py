"""Servers router."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.incident import IncidentStatus, IncidentType
from shared.contracts.dto.server import (
    ProvisioningAttemptReservation,
    ProvisioningAttemptReservationResult,
    ProvisioningAttemptReset,
    ProvisioningAttemptResetResult,
    ServerStatus,
    SSHUser,
    TargetIdentity,
    TargetReadinessRead,
    TargetReadinessReport,
)
from shared.contracts.queues.provisioner import ProvisionerMessage, ProvisioningProfile
from shared.crypto import SecretsCipher
from shared.models import Application, Incident, PortAllocation, Server
from shared.provisioning_policy import provider_operation_is_authorized
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.queues import PROVISIONER_QUEUE
from shared.redis.client import RedisStreamClient
from shared.server_admission import (
    ADMITTING_SERVER_STATUSES,
    TARGET_NOT_READY_STATUS,
    managed_row_requires_admin_key,
)
from shared.ssh_keys import (
    AdminKeyRejectedError,
    AdminPrivateKey,
    normalize_admin_private_key,
    validate_stored_admin_private_key,
)

from ..database import get_async_session
from ..dependencies import get_redis_client, require_internal_or_admin
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

_ACTIVE_INCIDENT_STATUSES = (IncidentStatus.DETECTED.value, IncidentStatus.RECOVERING.value)
# The step the QA runtime journals a host that lends no QA identity under. A
# successful reconciliation applied and proved exactly that identity, so it is
# the one provisioning-failure episode it may resolve.
_QA_IDENTITY_REFUSAL_STEP = "qa_identity"


class ProvisioningRequest(BaseModel):
    """One explicit provisioning invocation profile, if the caller needs one."""

    profile: ProvisioningProfile | None = None


def _admin_key_or_422(raw: str | None) -> AdminPrivateKey:
    """Parse a submitted administrative key, refusing it by reason and never by content."""
    try:
        return normalize_admin_private_key(raw)
    except AdminKeyRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"ssh_key rejected: {exc.rejection.value}",
        ) from None


def _stored_key_fingerprint(server: Server) -> str | None:
    """The public fingerprint of the key this row stores, or None if it has no usable one."""
    if not server.ssh_key_enc:
        return None
    try:
        return validate_stored_admin_private_key(
            SecretsCipher().decrypt(server.ssh_key_enc)
        ).fingerprint
    except AdminKeyRejectedError:
        return None


@router.post("/", response_model=ServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_in: ServerCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Create a new server (admin only).

    A submitted key is parsed before anything is added, and only its encrypted
    canonical text and public fingerprint are kept. A managed row is refused
    without one unless provisioning still owns it and will mint it.
    """
    if server_in.ssh_key is not None:
        admin_key = _admin_key_or_422(server_in.ssh_key)
    elif managed_row_requires_admin_key(
        is_managed=server_in.is_managed, status=server_in.status, labels=server_in.labels
    ):
        _admin_key_or_422(None)
    else:
        admin_key = None
    if await db.get(Server, server_in.handle):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server with this handle already exists",
        )

    server = Server(
        handle=server_in.handle,
        host=server_in.host,
        public_ip=server_in.public_ip,
        ssh_user=server_in.ssh_user,
        ssh_key_enc=SecretsCipher().encrypt(admin_key.text) if admin_key else None,
        ssh_key_fingerprint=admin_key.fingerprint if admin_key else None,
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


@router.post(
    "/{handle}/provisioning-attempts/reserve",
    response_model=ProvisioningAttemptReservationResult,
)
async def reserve_provisioning_attempt(
    handle: str,
    request: ProvisioningAttemptReservation,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> ProvisioningAttemptReservationResult:
    """Atomically reserve an attempt if the current episode has capacity."""
    new_episode_id = str(uuid4())
    statement = (
        update(Server)
        .where(
            Server.handle == handle,
            Server.provisioning_attempts < request.max_attempts,
        )
        .values(
            provisioning_attempts=Server.provisioning_attempts + 1,
            provisioning_episode_id=case(
                (Server.provisioning_attempts == 0, new_episode_id),
                else_=Server.provisioning_episode_id,
            ),
        )
        .returning(Server.provisioning_attempts, Server.provisioning_episode_id)
    )
    result = await db.execute(statement)
    reservation = result.one_or_none()
    if reservation is not None:
        attempts, episode_id = reservation
        if episode_id is None:
            raise RuntimeError("Provisioning episode id is missing for a reserved attempt")
        await db.commit()
        return ProvisioningAttemptReservationResult(
            reserved=True,
            provisioning_attempts=attempts,
            episode_id=episode_id,
        )

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return ProvisioningAttemptReservationResult(
        reserved=False,
        provisioning_attempts=server.provisioning_attempts,
        episode_id=server.provisioning_episode_id,
    )


@router.post(
    "/{handle}/provisioning-attempts/reset",
    response_model=ProvisioningAttemptResetResult,
)
async def reset_provisioning_attempts(
    handle: str,
    request: ProvisioningAttemptReset,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> ProvisioningAttemptResetResult:
    """Close an episode without erasing a newer reserved attempt."""
    statement = (
        update(Server)
        .where(
            Server.handle == handle,
            Server.provisioning_attempts == request.attempt_number,
            Server.provisioning_episode_id == request.episode_id,
        )
        .values(
            provisioning_attempts=0,
            provisioning_episode_id=None,
            status=ServerStatus.READY.value,
            # This write owns the status now; a readiness park no longer does.
            target_readiness_parked_status=None,
        )
        .returning(Server.provisioning_attempts, Server.provisioning_episode_id)
    )
    result = await db.execute(statement)
    reset = result.one_or_none()
    if reset is not None:
        attempts, episode_id = reset
        await db.commit()
        return ProvisioningAttemptResetResult(
            reset=True,
            provisioning_attempts=attempts,
            episode_id=episode_id,
        )

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return ProvisioningAttemptResetResult(
        reset=False,
        provisioning_attempts=server.provisioning_attempts,
        episode_id=server.provisioning_episode_id,
    )


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
        application_id=allocation_in.application_id,
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


def _refuse_a_keyless_managed_result(
    server: Server,
    updates: dict,
    *,
    replacement_key: AdminPrivateKey | None,
    clear_key: bool,
) -> None:
    """Refuse a request that would leave a managed row without the key it needs.

    Judged on the row the request would leave behind, so a promotion cannot clear
    the key in the same request that makes it required.
    """
    final_managed = bool(updates["is_managed"]) if "is_managed" in updates else server.is_managed
    if clear_key and final_managed:
        _admin_key_or_422(None)
    keyless_after = replacement_key is None and (clear_key or not server.ssh_key_enc)
    if (
        keyless_after
        and final_managed
        and not server.is_managed
        and managed_row_requires_admin_key(
            is_managed=True,
            status=updates.get("status", server.status),
            labels=updates.get("labels", server.labels),
        )
    ):
        _admin_key_or_422(None)


def _apply_key_change(
    server: Server, *, replacement_key: AdminPrivateKey | None, clear_key: bool
) -> bool:
    """Store or clear the key, and say whether the row's key identity changed."""
    if replacement_key is not None:
        changed = replacement_key.fingerprint != _stored_key_fingerprint(server)
        server.ssh_key_enc = SecretsCipher().encrypt(replacement_key.text)
        server.ssh_key_fingerprint = replacement_key.fingerprint
        return changed
    if clear_key:
        changed = server.ssh_key_enc is not None
        server.ssh_key_enc = None
        server.ssh_key_fingerprint = None
        return changed
    return False


@router.patch("/{handle}", response_model=ServerRead)
async def update_server(
    handle: str,
    updates: dict,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> Server:
    """Update server fields (admin only).

    The row is locked for the request and every rule is checked before any field
    is applied, so a refused request leaves the row exactly as it was:

    * a managed row may not lose its administrative key, and a keyless row may
      not be promoted into a managed state that needs one;
    * a change to the proved connection identity — key, `ssh_user`, `host` or
      `public_ip`, server-sync address updates included — clears the QA target
      readiness receipt in the same transaction, so admission fails closed until
      reconciliation proves the new identity;
    * any status write ends a readiness park's ownership of the row's status, so
      a later successful reconciliation never restores over it.
    """
    from datetime import datetime

    server = await db.get(Server, handle, with_for_update=True)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    replacement_key: AdminPrivateKey | None = None
    clear_key = False
    if "ssh_key" in updates:
        raw_key = updates.pop("ssh_key")
        if raw_key:
            replacement_key = _admin_key_or_422(raw_key)
        else:
            clear_key = True

    # Update allowed fields
    if "ssh_user" in updates:
        try:
            updates["ssh_user"] = TypeAdapter(SSHUser).validate_python(updates["ssh_user"])
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    if "provider" in updates or "provider_id" in updates:
        current_provider = server.provider if isinstance(server.provider, str) else None
        provider = updates.pop("provider", current_provider)
        provider_id = updates.pop("provider_id", server.provider_id)
        labels = dict(updates.get("labels", server.labels))
        if provider is None:
            labels.pop("provider", None)
        else:
            labels["provider"] = str(provider)
        if provider_id is None:
            labels.pop("provider_id", None)
        else:
            labels["provider_id"] = str(provider_id)
        updates["labels"] = labels

    _refuse_a_keyless_managed_result(
        server, updates, replacement_key=replacement_key, clear_key=clear_key
    )

    identity_before = (server.ssh_user, server.host, server.public_ip)
    key_changed = _apply_key_change(server, replacement_key=replacement_key, clear_key=clear_key)

    allowed_fields = {
        "host",
        "public_ip",
        "status",
        "notes",
        "is_managed",
        "labels",
        "ssh_user",
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

    if key_changed or (server.ssh_user, server.host, server.public_ip) != identity_before:
        server.qa_target_version = None
        server.qa_target_proved_at = None
    if "status" in updates:
        server.target_readiness_parked_status = None

    await db.commit()
    await db.refresh(server)
    return server


@router.post("/{handle}/target-readiness", response_model=TargetReadinessRead)
async def record_target_readiness(
    handle: str,
    report: TargetReadinessReport,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TargetReadinessRead:
    """Apply one managed-target readiness verdict in a single row-locked transaction.

    The verdict must have been proved over the connection identity the row has
    now: administrative user, host, public address and stored-key fingerprint. A
    verdict for any other identity is refused with 409 and changes nothing, so a
    change that lands during a reconciliation never receives its receipt or park.

    Readiness owns its own evidence and nothing else. A failure clears the
    receipt, records its phase on the row, creates or updates the one active
    `target_not_ready` incident, and moves the row to `error` only out of an
    admitting status, remembering that status as its park. A success writes the
    receipt, resolves that incident and the QA runtime's `qa_identity` refusals
    it repairs, and restores the parked status only while the park still owns the
    row's `error`. Other provisioning failures, and statuses written by anything
    else, are never overwritten or cleared. The encrypted key is never touched.
    """
    server = await db.get(Server, handle, with_for_update=True)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    if not server.is_managed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target readiness is recorded only for managed servers",
        )
    current_identity = TargetIdentity(
        ssh_user=server.ssh_user,
        host=server.host,
        public_ip=server.public_ip,
        ssh_key_fingerprint=_stored_key_fingerprint(server),
    )
    if report.identity != current_identity:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The verdict was proved over a connection identity this server no longer has",
        )
    if report.ready and report.profile_version != QA_TARGET_PROFILE_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"QA target profile {report.profile_version} is not the current profile "
                f"{QA_TARGET_PROFILE_VERSION}"
            ),
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    readiness_incident = (
        (
            await db.execute(
                select(Incident)
                .where(
                    Incident.server_handle == handle,
                    Incident.incident_type == IncidentType.TARGET_NOT_READY.value,
                    Incident.status.in_(_ACTIVE_INCIDENT_STATUSES),
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    incident_id: int | None = None
    if report.ready:
        server.qa_target_version = report.profile_version
        server.qa_target_proved_at = report.proved_at.astimezone(UTC).replace(tzinfo=None)
        if readiness_incident is not None:
            readiness_incident.status = IncidentStatus.RESOLVED.value
            readiness_incident.resolved_at = now
        provisioning_failures = (
            (
                await db.execute(
                    select(Incident)
                    .where(
                        Incident.server_handle == handle,
                        Incident.incident_type == IncidentType.PROVISIONING_FAILED.value,
                        Incident.status.in_(_ACTIVE_INCIDENT_STATUSES),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for incident in provisioning_failures:
            if (incident.details or {}).get("step") == _QA_IDENTITY_REFUSAL_STEP:
                incident.status = IncidentStatus.RESOLVED.value
                incident.resolved_at = now
        if (
            server.target_readiness_parked_status is not None
            and server.status == TARGET_NOT_READY_STATUS.value
        ):
            server.status = server.target_readiness_parked_status
        server.target_readiness_parked_status = None
        server.target_readiness_failure_phase = None
    else:
        server.qa_target_version = None
        server.qa_target_proved_at = None
        server.target_readiness_failure_phase = report.phase.value
        if server.status in {state.value for state in ADMITTING_SERVER_STATUSES}:
            server.target_readiness_parked_status = server.status
            server.status = TARGET_NOT_READY_STATUS.value
        details = {
            "step": "target_readiness",
            "phase": report.phase.value,
            "detail": report.detail,
            "revision": report.revision,
            "server_handle": handle,
            "repair": f"python -m src.provisioner.qa_identity_retrofit {handle}",
        }
        if readiness_incident is None:
            readiness_incident = Incident(
                server_handle=handle,
                incident_type=IncidentType.TARGET_NOT_READY.value,
                status=IncidentStatus.DETECTED.value,
                details=details,
                affected_services=[],
                recovery_attempts=0,
            )
            db.add(readiness_incident)
            await db.flush()
        else:
            readiness_incident.details = details
            readiness_incident.recovery_attempts = (readiness_incident.recovery_attempts or 0) + 1
        incident_id = readiness_incident.id

    await db.commit()
    return TargetReadinessRead(
        server_handle=handle,
        ready=report.ready,
        status=ServerStatus(server.status),
        qa_target_version=server.qa_target_version,
        qa_target_proved_at=server.qa_target_proved_at,
        target_readiness_failure_phase=server.target_readiness_failure_phase,
        incident_id=incident_id,
    )


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

    if not provider_operation_is_authorized(
        provider=server.provider,
        provider_id=server.provider_id,
        is_managed=server.is_managed,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Server is not authorized for provisioning",
        )

    server.status = ServerStatus.FORCE_REBUILD.value
    server.target_readiness_parked_status = None
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
    request: ProvisioningRequest | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Queue the existing provisioner path without claiming a terminal state."""

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    message = ProvisionerMessage(server_handle=handle, profile=request.profile if request else None)
    await redis.publish_message(PROVISIONER_QUEUE, message)
    return {"request_id": message.request_id, "server_handle": handle}


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


@router.get("/{handle}/monitoring-status")
async def get_monitoring_status(
    handle: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Return the latest monitoring observations for an existing server."""
    from shared.models import ServerMetricsHistory

    server = await db.get(Server, handle)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    latest_metrics = await db.scalar(
        select(ServerMetricsHistory.recorded_at)
        .where(ServerMetricsHistory.server_handle == handle)
        .order_by(ServerMetricsHistory.recorded_at.desc())
        .limit(1)
    )
    baseline_applied_at = (server.labels or {}).get("monitoring_baseline_applied_at")

    if baseline_applied_at is None and latest_metrics is None:
        monitoring_state = "not_provisioned"
    elif latest_metrics is None:
        monitoring_state = "baseline_applied_waiting_for_metrics"
    else:
        monitoring_state = "metrics_observed"

    return {
        "server_handle": handle,
        "monitoring_baseline_applied_at": baseline_applied_at,
        "exporter_last_observed_reachable_at": server.last_health_check,
        "metrics_last_collected_at": latest_metrics,
        "state": monitoring_state,
    }


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
