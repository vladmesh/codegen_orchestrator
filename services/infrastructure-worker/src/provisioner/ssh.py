"""SSH utilities for provisioner."""

import os
import subprocess
import time

import structlog

logger = structlog.get_logger()


def get_ssh_public_key() -> str | None:
    """Read SSH public key from default location.

    Returns:
        Public key string or None if not found
    """
    key_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    return None


def check_ssh_access(server_ip: str, timeout: int = 10) -> bool:
    """Check if server is accessible via SSH key.

    Args:
        server_ip: Server IP address
        timeout: Check timeout in seconds

    Returns:
        True if accessible via SSH key
    """
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        f"root@{server_ip}",
        "echo success",
    ]

    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        success = result.returncode == 0 and "success" in result.stdout
        duration_ms = (time.time() - start) * 1000
        logger.info(
            "ssh_connection_test",
            host=server_ip,
            success=success,
            duration_ms=round(duration_ms, 2),
        )
        return success
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        logger.warning(
            "ssh_connection_test_failed",
            host=server_ip,
            duration_ms=round(duration_ms, 2),
            error=str(e),
            error_type=type(e).__name__,
        )
        return False
