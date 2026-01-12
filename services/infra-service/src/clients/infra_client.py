"""Infrastructure client for SSH-based container inspection.

Extends the SSH utilities from provisioner to provide container management.
Phase 4.4 addition for Diagnose capability.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

from ..config.constants import Paths, Timeouts

logger = structlog.get_logger(__name__)

# SSH configuration - use centralized constants
SSH_KEY_PATH = Paths.SSH_KEY
SSH_TIMEOUT = Timeouts.SSH_COMMAND


async def run_ssh_command(
    server_ip: str,
    command: str,
    timeout: int = SSH_TIMEOUT,
) -> tuple[bool, str, str]:
    """Run a command on a remote server via SSH.

    Args:
        server_ip: Server IP address
        command: Command to run
        timeout: Timeout in seconds

    Returns:
        Tuple of (success, stdout, stderr)
    """
    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={min(timeout, 10)}",
        "-i",
        SSH_KEY_PATH,
        f"root@{server_ip}",
        command,
    ]

    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )

        success = process.returncode == 0
        return success, stdout.decode(), stderr.decode()

    except TimeoutError:
        logger.warning("ssh_command_timeout", server_ip=server_ip, timeout=timeout)
        return False, "", "Command timed out"
    except Exception as e:
        logger.error("ssh_command_error", server_ip=server_ip, error=str(e))
        return False, "", str(e)


async def get_container_logs(
    server_ip: str,
    container_name: str,
    lines: int = 100,
    since: str | None = None,
) -> dict:
    """Get logs from a Docker container via SSH.

    Args:
        server_ip: Server IP address
        container_name: Container/service name
        lines: Number of lines to fetch
        since: ISO timestamp to get logs from (optional)

    Returns:
        {"logs": "...", "success": True/False, "error": "..."}
    """
    lines = min(lines, 1000)

    cmd = f"docker logs --tail {lines}"
    if since:
        cmd += f" --since {since}"
    cmd += f" {container_name} 2>&1"

    success, stdout, stderr = await run_ssh_command(server_ip, cmd)

    if success or stdout:  # docker logs outputs to stdout even on "error"
        return {
            "logs": stdout or stderr,
            "success": True,
            "lines_returned": len((stdout or stderr).split("\n")),
        }
    else:
        return {
            "logs": "",
            "success": False,
            "error": stderr or "Failed to fetch logs",
        }


async def get_container_status(
    server_ip: str,
    container_name: str,
) -> dict:
    """Get status of a Docker container via SSH.

    Args:
        server_ip: Server IP address
        container_name: Container/service name

    Returns:
        {
            "status": "running|exited|not_found",
            "uptime": "2h 15m",
            "health": "healthy|unhealthy|none"
        }
    """
    # Get container status with format - break into parts for readability
    format_str = "'{{{{.State.Status}}}}|{{{{.State.Health.Status}}}}|{{{{.State.StartedAt}}}}'"
    cmd = f"docker inspect --format {format_str} {container_name} 2>/dev/null || echo 'not_found'"

    success, stdout, stderr = await run_ssh_command(server_ip, cmd)

    if not success or stdout.strip() == "not_found":
        return {
            "status": "not_found",
            "error": f"Container {container_name} not found on {server_ip}",
        }

    parts = stdout.strip().split("|")
    min_parts_for_started_at = 3
    status = parts[0] if len(parts) > 0 else "unknown"
    health = parts[1] if len(parts) > 1 and parts[1] else "none"
    started_at = parts[2] if len(parts) >= min_parts_for_started_at else None

    # Calculate uptime
    uptime = None
    if started_at and status == "running":
        from datetime import datetime

        try:
            # Parse ISO timestamp (Docker format)
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = datetime.now(UTC) - started
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            uptime = f"{hours}h {minutes}m"
        except Exception:
            uptime = "unknown"

    return {
        "status": status,
        "health": health,
        "uptime": uptime,
        "started_at": started_at,
    }


async def get_container_stats(
    server_ip: str,
    container_name: str,
) -> dict:
    """Get resource usage stats for a container.

    Args:
        server_ip: Server IP address
        container_name: Container/service name

    Returns:
        {"cpu_percent": 5.2, "memory_mb": 128, "memory_limit_mb": 512}
    """
    format_str = "'{{{{.CPUPerc}}}}|{{{{.MemUsage}}}}'"
    cmd_base = f"docker stats --no-stream --format {format_str} {container_name}"
    cmd = f"{cmd_base} 2>/dev/null || echo 'not_found'"

    success, stdout, stderr = await run_ssh_command(server_ip, cmd)

    if not success or stdout.strip() == "not_found":
        return {"error": f"Container {container_name} not found"}

    parts = stdout.strip().split("|")
    cpu_str = parts[0] if len(parts) > 0 else "0%"
    mem_str = parts[1] if len(parts) > 1 else "0MiB / 0MiB"

    # Parse CPU percentage
    try:
        cpu_percent = float(cpu_str.rstrip("%"))
    except ValueError:
        cpu_percent = 0.0

    # Parse memory usage (format: "128MiB / 512MiB")
    memory_mb = 0
    memory_limit_mb = 0
    try:
        mem_parts = mem_str.split(" / ")
        # Remove memory unit suffixes
        mem_value = mem_parts[0].replace("MiB", "").replace("GiB", "")
        memory_mb = float(mem_value)
        if "GiB" in mem_parts[0]:
            memory_mb *= 1024
        limit_value = mem_parts[1].replace("MiB", "").replace("GiB", "")
        memory_limit_mb = float(limit_value)
        if "GiB" in mem_parts[1]:
            memory_limit_mb *= 1024
    except (ValueError, IndexError):
        pass

    return {
        "cpu_percent": cpu_percent,
        "memory_mb": int(memory_mb),
        "memory_limit_mb": int(memory_limit_mb),
    }


async def check_http_health(
    url: str,
    timeout: int = 5,
) -> dict:
    """Check HTTP health endpoint.

    Args:
        url: Full URL to check (e.g., http://1.2.3.4:8080/health)
        timeout: Request timeout in seconds

    Returns:
        {"healthy": True/False, "status_code": 200, "response_time_ms": 45}
    """
    import time

    import httpx

    start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout)

        response_time_ms = int((time.time() - start) * 1000)

        return {
            "healthy": response.is_success,
            "status_code": response.status_code,
            "response_time_ms": response_time_ms,
        }

    except httpx.TimeoutException:
        return {
            "healthy": False,
            "error": "Request timed out",
            "response_time_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "healthy": False,
            "error": str(e),
            "response_time_ms": int((time.time() - start) * 1000),
        }
