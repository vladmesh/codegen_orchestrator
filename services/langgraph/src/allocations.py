"""Shared resource allocation logic.

This module provides a reusable function for allocating server resources
(finding a suitable server and allocating ports) for a project.

Used by:
- ResourceAllocatorNode (engineering flow)
- deploy_worker (deploy flow)
"""

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.server import ServerDTO, ServerStatus

from .clients.api import api_client
from .config.settings import get_settings
from .schemas.api_types import AllocationInfo

logger = structlog.get_logger(__name__)


class AllocationError(Exception):
    """Raised when resource allocation fails."""


async def ensure_project_allocations(
    project_id: str,
    repo_id: str,
    service_name: str,
    modules: list[str] | None = None,
    min_ram_mb: int = 512,
    min_disk_mb: int = 1024,
) -> dict[str, dict]:
    """Ensure a project has resource allocations, creating them if needed.

    This is the single source of truth for allocation logic. It:
    1. Gets or creates an Application for the repo+server
    2. Checks if allocations already exist for the application
    3. If yes, returns existing allocations
    4. If no, finds a suitable server and allocates ports

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

    # Find suitable server first (needed for Application creation)
    server = await _find_suitable_server(min_ram_mb, min_disk_mb)

    server_handle = server.handle
    server_ip = server.public_ip

    # Get or create Application
    app = await api_client.get_or_create_application(
        repo_id=repo_id,
        server_handle=server_handle,
        service_name=service_name,
        reserved_ram_mb=min_ram_mb,
    )
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


async def _find_suitable_server(min_ram_mb: int, min_disk_mb: int) -> ServerDTO:
    """Find a server that can admit the requested RAM allocation conservatively.

    Admission reserves ``min_ram_mb + ALLOCATION_RAM_RESERVE_MB``. It compares that
    budget against both the persisted sum of application reservations and fresh
    observed RAM use, then uses the larger value. Metrics older than
    ``ALLOCATION_METRICS_FRESHNESS_SECONDS`` (or absent) are unknown and reject the
    server. A rejection reports whether every candidate lacked capacity, fresh
    metrics, or observed free memory.
    """
    servers = await api_client.list_servers(is_managed=True)
    settings = get_settings()
    required_ram_mb = min_ram_mb + settings.allocation_ram_reserve_mb

    # Filter to only active/ready/in_use servers
    active_statuses = (ServerStatus.ACTIVE, ServerStatus.READY, ServerStatus.IN_USE)
    servers = [s for s in servers if s.status in active_statuses]

    suitable: list[tuple[ServerDTO, int]] = []
    rejection_reasons: set[str] = set()
    for srv in servers:
        if srv.capacity_disk_mb < min_disk_mb:
            rejection_reasons.add("insufficient_capacity")
            continue

        applications = await api_client.list_applications({"server_handle": srv.handle})
        reserved_ram_mb = sum(app.get("reserved_ram_mb", 0) for app in applications)
        if srv.capacity_ram_mb < reserved_ram_mb + required_ram_mb:
            rejection_reasons.add("insufficient_capacity")
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
        reason = _allocation_failure_reason(rejection_reasons)
        raise AllocationError(f"No suitable server found: {reason}")

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


def _allocation_failure_reason(rejection_reasons: set[str]) -> str:
    """Return all stable, actionable causes for an empty candidate set."""
    reasons = [
        reason
        for reason in ("no_fresh_metrics", "insufficient_free_memory", "insufficient_capacity")
        if reason in rejection_reasons
    ]
    return ", ".join(reasons) if reasons else "insufficient_capacity"
