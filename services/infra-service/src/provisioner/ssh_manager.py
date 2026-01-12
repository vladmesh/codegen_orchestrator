"""SSH Key Management for server access."""

import os
import subprocess
import time

import structlog

logger = structlog.get_logger()


class SSHManager:
    """Manages SSH keys and connectivity checking."""

    def __init__(self, key_path: str | None = None):
        if key_path:
            self.key_path = key_path
        else:
            self.key_path = os.path.expanduser("~/.ssh/id_ed25519")

        self.pub_key_path = f"{self.key_path}.pub"

    def ensure_keys_exist(self) -> None:
        """Ensure SSH keys exist, generating them if necessary."""
        if os.path.exists(self.key_path) and os.path.exists(self.pub_key_path):
            return

        logger.info("ssh_key_generation_start", path=self.key_path)

        # Ensure directory exists
        os.makedirs(os.path.dirname(self.key_path), exist_ok=True)

        try:
            subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    self.key_path,
                    "-N",
                    "",  # No passphrase
                    "-C",
                    "orchestrator-generated-key",
                ],
                check=True,
                capture_output=True,
            )
            logger.info("ssh_key_generated")
        except subprocess.CalledProcessError as e:
            logger.error("ssh_key_generation_failed", error=str(e), stderr=e.stderr)
            raise RuntimeError(f"Failed to generate SSH keys: {e}") from e

    def get_public_key(self) -> str | None:
        """Read SSH public key."""
        self.ensure_keys_exist()

        if os.path.exists(self.pub_key_path):
            with open(self.pub_key_path) as f:
                return f.read().strip()
        return None

    def check_ssh_access(self, server_ip: str, timeout: int = 10) -> bool:
        """Check if server is accessible via SSH key.

        Args:
            server_ip: Server IP address
            timeout: Check timeout in seconds

        Returns:
            True if accessible via SSH key
        """
        self.ensure_keys_exist()

        cmd = [
            "ssh",
            "-i",
            self.key_path,
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

            log_method = logger.info if success else logger.info
            log_method(
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
