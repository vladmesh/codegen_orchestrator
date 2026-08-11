"""Shared resource allocation logic.

This module provides a reusable function for allocating server resources
(finding a suitable server and allocating ports) for a project.

Used by:
- ResourceAllocatorNode (engineering flow)
- deploy_worker (deploy flow)
"""

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.application import DEFAULT_APPLICATION_RESERVED_RAM_MB, ApplicationStatus
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.server import ServerDTO
from shared.server_admission import (
    ADMISSION_FAILURE_REASON,
    ServerAdmissionRejection,
    provisioning_failed_server_handles,
    server_admission_rejection,
)

from .clients.api import api_client
from .config.settings import get_settings
from .schemas.api_types import AllocationInfo

logger = structlog.get_logger(__name__)

DEFAULT_ALLOCATION_MIN_DISK_MB = 1024


class AllocationError(Exception):
    """Raised when resource allocation fails.

    Carries the admission budget the refused attempt asked for, so every caller
    can record what has to become available again without recomputing it — and
    so the classification cannot be reduced to a message on the way out.
    """

    def __init__(
        self,
        reason: AllocationFailureReason,
        *,
        required_ram_mb: int,
        min_disk_mb: int,
        message: str | None = None,
    ) -> None:
        self.reason = reason
        self.required_ram_mb = required_ram_mb
        self.min_disk_mb = min_disk_mb
        super().__init__(message or f"No suitable server found: {reason.value}")


async def ensure_project_allocations(
    project_id: str,
    repo_id: str,
    service_name: str,
    modules: list[str] | None = None,
    min_ram_mb: int = DEFAULT_APPLICATION_RESERVED_RAM_MB,
    min_disk_mb: int = DEFAULT_ALLOCATION_MIN_DISK_MB,
) -> dict[str, dict]:
    """Ensure a project has resource allocations, creating them if needed.

    This is the single source of truth for allocation logic. It:
    1. Gets or creates an Application for the repo+server
    2. Checks if allocations already exist for the application
    3. If yes, returns existing allocations
    4. If no, finds a suitable server and allocates ports

    Both branches pass ``shared.server_admission``: a newly chosen host through
    :func:`_find_suitable_server`, an already bound host through
    :func:`_refuse_inadmissible_target`. Reuse is placement too — a redeploy runs
    the application on that host again and a newly declared module takes a fresh
    port on it — so the same predicate decides both, and neither can admit what
    the other refuses.

    Args:
        project_id: Project ID (for finding server/repo)
        repo_id: Repository ID for the Application
        service_name: Human-readable name (e.g. "fortune-teller-bot")
        modules: List of modules needing ports (default: ["backend"])
        min_ram_mb: Minimum RAM required
        min_disk_mb: Minimum disk required

    Returns:
        Dict of allocations keyed by "server_handle:port"

    Raises:
        AllocationError: If allocation fails
    """
    if modules is None:
        modules = ["backend"]

    # A placement already exists for this repository. Its reservation and observed
    # memory are already included in server load, so reuse skips the *capacity*
    # budget — but never admission: the bound host still has to be a legal target
    # before it takes this project's workload again.
    existing_apps = await api_client.list_applications({"repo_id": repo_id})
    if existing_apps:
        app = existing_apps[0]
        server_handle = app["server_handle"]
        server = await api_client.get_server(server_handle)
        _refuse_inadmissible_target(
            server,
            provisioning_failed_server_handles(await api_client.list_active_incidents()),
            min_ram_mb=min_ram_mb,
            min_disk_mb=min_disk_mb,
        )
    else:
        server = await _find_suitable_server(min_ram_mb, min_disk_mb)
        server_handle = server.handle
        app = await api_client.get_or_create_application(
            repo_id=repo_id,
            server_handle=server_handle,
            service_name=service_name,
            reserved_ram_mb=min_ram_mb,
        )

    server_ip = server.public_ip
    application_id = app["id"]

    # Check for existing allocations on this application
    existing: list[AllocationInfo] = await api_client.get_application_allocations(application_id)
    allocated: dict[str, dict] = {}
    if existing:
        logger.info(
            "allocations_already_exist",
            application_id=application_id,
            count=len(existing),
        )
        for alloc in existing:
            alloc_server = alloc["server_handle"]
            port = alloc["port"]
            key = f"{alloc_server}:{port}"

            alloc_ip = alloc.get("server_ip")
            if not alloc_ip:
                srv: ServerDTO = await api_client.get_server(alloc_server)
                alloc_ip = srv.public_ip

            allocated[key] = {
                "port": port,
                "server_handle": alloc_server,
                "server_ip": alloc_ip,
                "service_name": alloc.get("service_name"),
                "application_id": application_id,
            }

    existing_services = {alloc["service_name"] for alloc in allocated.values()}
    missing_modules = [
        module for module in dict.fromkeys(modules) if module not in existing_services
    ]
    if not missing_modules:
        return allocated

    # No existing allocations - create new ones
    logger.info(
        "allocating_resources",
        application_id=application_id,
        modules=missing_modules,
        min_ram_mb=min_ram_mb,
    )

    # Allocate port for each module atomically
    for module in missing_modules:
        alloc_result = await api_client.allocate_next_port(
            server_handle,
            {
                "service_name": module,
                "application_id": application_id,
            },
        )
        port = alloc_result["port"]
        key = f"{server_handle}:{port}"
        allocated[key] = {
            "port": port,
            "server_handle": server_handle,
            "server_ip": server_ip,
            "service_name": module,
            "application_id": application_id,
        }

        logger.info(
            "port_allocated",
            application_id=application_id,
            module=module,
            server=server_handle,
            port=port,
        )

    return allocated


def _refuse_inadmissible_target(
    server: ServerDTO,
    provisioning_failed_handles: frozenset[str],
    *,
    min_ram_mb: int,
    min_disk_mb: int,
) -> None:
    """Refuse to put more of a project on a server that may not host one.

    This is the admission decision for an already bound host, and it is the same
    decision `_find_suitable_server` makes for a new one: `server_admission_rejection`
    from `shared.server_admission`, over the same snapshot of active incidents.
    A binding made while the host was legal is not a licence to keep placing work
    on it after its provisioning restarted or broke.

    The refusal is the existing typed `AllocationError` carrying the admission
    budget, so it travels the route every other refusal travels
    (`shared.allocation_disposition`): a bounded infrastructure wait that ends
    with a human, never a capacity message to the owner and never a story
    failure.

    Every rejection is reported as `shared.server_admission.ADMISSION_FAILURE_REASON`,
    the constant the search path raises too — the reasoning for one reason
    covering all four lives there, next to the rejections themselves.
    """
    admission = server_admission_rejection(server, provisioning_failed_handles)
    if admission is None:
        return
    logger.info(
        "server_admission_rejected",
        server=server.handle,
        status=server.status.value,
        reason=admission.value,
        placement="reuse",
    )
    raise AllocationError(
        ADMISSION_FAILURE_REASON,
        required_ram_mb=allocation_required_ram_mb(min_ram_mb),
        min_disk_mb=min_disk_mb,
        message=(f"Bound server {server.handle} may not host an application: {admission.value}"),
    )


async def _find_suitable_server(min_ram_mb: int, min_disk_mb: int) -> ServerDTO:
    """Find a server that can admit the requested RAM allocation conservatively.

    A candidate first has to be an admissible target at all: managed, operational,
    software provisioning recorded complete, and free of an open provisioning
    failure. That rule lives in ``shared.server_admission`` and is shared with the
    scheduler's resource wait, so the two cannot diverge. A host that fails it is
    never described as a capacity problem — whichever rejection it was, the
    refusal carries ``shared.server_admission.ADMISSION_FAILURE_REASON``.

    Admission then reserves ``min_ram_mb + ALLOCATION_RAM_RESERVE_MB``. It compares
    that budget against both the persisted sum of application reservations and
    fresh observed RAM use, then uses the larger value. Metrics older than
    ``ALLOCATION_METRICS_FRESHNESS_SECONDS`` (or absent) are unknown and reject the
    server. A rejection reports whether every candidate was unprovisioned, or
    lacked capacity, fresh metrics, or observed free memory.
    """
    all_managed_servers = await api_client.list_servers(is_managed=True)
    settings = get_settings()
    required_ram_mb = allocation_required_ram_mb(min_ram_mb)
    provisioning_failed_handles = provisioning_failed_server_handles(
        await api_client.list_active_incidents()
    )

    suitable: list[tuple[ServerDTO, int]] = []
    rejection_reasons: set[str] = set()
    admission_rejections: set[ServerAdmissionRejection] = set()
    for srv in all_managed_servers:
        admission = server_admission_rejection(srv, provisioning_failed_handles)
        if admission is not None:
            admission_rejections.add(admission)
            logger.info(
                "server_admission_rejected",
                server=srv.handle,
                status=srv.status.value,
                reason=admission.value,
                placement="search",
            )
            continue

        if srv.capacity_disk_mb < min_disk_mb:
            rejection_reasons.add("insufficient_capacity")
            continue

        applications = await api_client.list_applications({"server_handle": srv.handle})
        reserved_ram_mb = sum(
            app["reserved_ram_mb"] for app in applications if _holds_ram_reservation(app)
        )
        if srv.capacity_ram_mb < reserved_ram_mb + required_ram_mb:
            rejection_reasons.add("insufficient_reserved_memory")
            continue

        if not _has_fresh_metrics(
            srv.last_health_check, settings.allocation_metrics_freshness_seconds
        ):
            rejection_reasons.add("no_fresh_metrics")
            continue

        observed_ram_mb = srv.used_ram_mb
        effective_used_ram_mb = max(reserved_ram_mb, observed_ram_mb)
        if srv.capacity_ram_mb < effective_used_ram_mb + required_ram_mb:
            rejection_reasons.add("insufficient_free_memory")
            continue

        suitable.append((srv, srv.capacity_ram_mb - effective_used_ram_mb))

    if not suitable:
        budget = {"required_ram_mb": required_ram_mb, "min_disk_mb": min_disk_mb}
        if _request_exceeds_every_server(all_managed_servers, required_ram_mb, min_disk_mb):
            raise AllocationError(AllocationFailureReason.IMPOSSIBLE_CAPACITY, **budget)
        # A host that may not take an application is infrastructure, not capacity,
        # whichever of the rejections it was: a build still running, a build that
        # broke, a host that left the admitting statuses, a host that stopped being
        # managed. None of them is a statement about how much memory was asked
        # for, so none of them may fall through to a memory reason below.
        if admission_rejections:
            refused = ", ".join(sorted(rejection.value for rejection in admission_rejections))
            raise AllocationError(
                ADMISSION_FAILURE_REASON,
                **budget,
                message=(
                    "No server may host an application: "
                    f"{ADMISSION_FAILURE_REASON.value} ({refused})"
                ),
            )
        # Unknown metrics cannot truthfully be described to a user as capacity.
        if "no_fresh_metrics" in rejection_reasons:
            raise AllocationError(AllocationFailureReason.NO_FRESH_METRICS, **budget)
        if "insufficient_reserved_memory" in rejection_reasons:
            raise AllocationError(AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY, **budget)
        raise AllocationError(AllocationFailureReason.INSUFFICIENT_FREE_MEMORY, **budget)

    # Prefer the most remaining RAM after the conservative admission budget.
    return max(suitable, key=lambda candidate: candidate[1])[0]


def _has_fresh_metrics(last_health_check: datetime | None, freshness_seconds: int) -> bool:
    """Return whether server metrics are recent enough to use for admission."""
    if last_health_check is None:
        return False
    if last_health_check.tzinfo is None:
        last_health_check = last_health_check.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - last_health_check).total_seconds()
    return 0 <= age_seconds <= freshness_seconds


def allocation_required_ram_mb(min_ram_mb: int) -> int:
    """Return the allocator's full RAM admission budget for a project request."""
    return min_ram_mb + get_settings().allocation_ram_reserve_mb


def _request_exceeds_every_server(
    servers: list[ServerDTO], required_ram_mb: int, min_disk_mb: int
) -> bool:
    """Return whether no managed active server could ever fit this request."""
    return not any(
        server.capacity_ram_mb >= required_ram_mb and server.capacity_disk_mb >= min_disk_mb
        for server in servers
    )


def _holds_ram_reservation(application: dict) -> bool:
    """Return whether an application can still consume server RAM.

    An undeployed or stopped application has no running workload and does not
    reserve admission capacity. Transitional and unhealthy states remain
    reserved conservatively until the application is explicitly stopped.
    """
    return application["status"] not in {
        ApplicationStatus.NOT_DEPLOYED.value,
        ApplicationStatus.STOPPED.value,
    }
