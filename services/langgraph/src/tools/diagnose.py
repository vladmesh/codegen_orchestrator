"""Diagnose capability tools for Dynamic ProductOwner.

Provides tools to view logs, check health, and debug issues.
Phase 4.4 addition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from langchain_core.tools import tool
import structlog

from ..clients import infra_client
from .base import api_client

logger = structlog.get_logger(__name__)


@tool
async def get_service_logs(
    project_id: Annotated[str, "Project ID to get logs for"],
    lines: Annotated[int, "Number of log lines to return"] = 100,
    since: Annotated[str | None, "ISO timestamp to get logs from (optional)"] = None,
) -> dict:
    """Fetch logs from a running service.

    Args:
        project_id: Project identifier
        lines: Number of lines (default 100, max 500)
        since: ISO timestamp to fetch logs from (optional)

    Returns:
        {"logs": "...", "source": "docker", "server": "vps-xxx"}
    """
    lines = min(lines, 500)

    # Get project allocations to find where it's deployed
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"error": "Project has no deployed resources", "logs": ""}

    allocation = allocations[0]
    server_handle = allocation.get("server_handle")

    # Get server IP
    server = await api_client.get_server(server_handle)
    if not server:
        return {"error": f"Server {server_handle} not found", "logs": ""}

    server_ip = server.get("public_ip")
    if not server_ip:
        return {"error": f"Server {server_handle} has no IP", "logs": ""}

    # Get project name for container name
    project = await api_client.get_project(project_id)
    container_name = project.get("name", project_id) if project else project_id

    # Fetch logs via SSH
    result = await infra_client.get_container_logs(
        server_ip=server_ip,
        container_name=container_name,
        lines=lines,
        since=since,
    )

    return {
        "logs": result.get("logs", ""),
        "source": "docker",
        "server": server_handle,
        "lines_returned": result.get("lines_returned", 0),
        "error": result.get("error"),
    }


@tool
async def check_service_health(
    project_id: Annotated[str, "Project ID to check health for"],
) -> dict:
    """Run health checks on a deployed service.

    Checks:
    - HTTP endpoint (/health)
    - Container status
    - Resource usage (CPU, memory)

    Returns:
        {
            "healthy": True/False,
            "checks": {
                "http": {"status": "ok", "response_time_ms": 45},
                "container": {"status": "running", "uptime": "2h 15m"},
                "resources": {"cpu_percent": 5.2, "memory_mb": 128}
            },
            "url": "http://1.2.3.4:8080"
        }
    """
    # Get project allocations
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"error": "Project not deployed", "healthy": False}

    allocation = allocations[0]
    server_handle = allocation.get("server_handle")
    port = allocation.get("port")

    # Get server info
    server = await api_client.get_server(server_handle)
    if not server:
        return {"error": f"Server {server_handle} not found", "healthy": False}

    server_ip = server.get("public_ip")
    if not server_ip:
        return {"error": f"Server {server_handle} has no IP", "healthy": False}

    # Get project name for container name
    project = await api_client.get_project(project_id)
    container_name = project.get("name", project_id) if project else project_id

    url = f"http://{server_ip}:{port}"
    checks = {}
    healthy = True

    # 1. HTTP health check
    http_result = await infra_client.check_http_health(f"{url}/health")
    checks["http"] = http_result
    if not http_result.get("healthy"):
        healthy = False

    # 2. Container status check
    container_result = await infra_client.get_container_status(server_ip, container_name)
    checks["container"] = container_result
    if container_result.get("status") != "running":
        healthy = False

    # 3. Resource usage
    stats_result = await infra_client.get_container_stats(server_ip, container_name)
    checks["resources"] = stats_result

    return {
        "healthy": healthy,
        "checks": checks,
        "url": url,
        "server": server_handle,
    }


@tool
async def get_error_history(
    project_id: Annotated[str, "Project ID to get errors for"],
    hours: Annotated[int, "How many hours back to look"] = 24,
) -> dict:
    """Get recent errors from service logs.

    Scans logs for error patterns and groups them.

    Args:
        project_id: Project identifier
        hours: How far back to look (default 24, max 168)

    Returns:
        {
            "errors": [
                {"message": "...", "count": 5, "first_seen": "...", "last_seen": "..."},
                ...
            ],
            "total_errors": 12
        }
    """
    hours = min(hours, 168)  # Max 1 week
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

    # Get logs
    logs_result = await get_service_logs.ainvoke(
        {
            "project_id": project_id,
            "lines": 500,
            "since": since,
        }
    )

    if logs_result.get("error"):
        return {"errors": [], "total_errors": 0, "error": logs_result["error"]}

    logs = logs_result.get("logs", "")

    # Parse and find errors
    error_lines = []
    for line in logs.split("\n"):
        line_lower = line.lower()
        if any(keyword in line_lower for keyword in ["error", "exception", "failed", "critical"]):
            error_lines.append(line)

    # Group similar errors (simple approach: first 100 chars)
    error_groups = {}
    for line in error_lines:
        key = line[:100].strip()
        if key not in error_groups:
            error_groups[key] = {"message": line, "count": 0, "occurrences": []}
        error_groups[key]["count"] += 1

    # Sort by count
    sorted_errors = sorted(
        error_groups.values(),
        key=lambda x: x["count"],
        reverse=True,
    )[:20]  # Top 20

    return {
        "errors": [{"message": e["message"], "count": e["count"]} for e in sorted_errors],
        "total_errors": len(error_lines),
        "time_range_hours": hours,
    }
