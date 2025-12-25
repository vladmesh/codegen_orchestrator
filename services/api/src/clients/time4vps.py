"""Time4VPS Client for Internal API."""

import asyncio
import base64
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Time4VPSClient:
    """Client for Time4VPS API."""

    def __init__(self, username: str, password: str):
        self.base_url = "https://billing.time4vps.com/api"
        self.username = username
        self.password = password
        self._auth_header: str | None = None

    def _get_auth_header(self) -> dict[str, str]:
        """Construct Basic Auth header."""
        if not self._auth_header:
            auth_str = f"{self.username}:{self.password}"
            encoded_auth = base64.b64encode(auth_str.encode()).decode()
            self._auth_header = f"Basic {encoded_auth}"
        return {"Authorization": self._auth_header}

    async def get_servers(self) -> list[dict[str, Any]]:
        """List all servers."""
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_server_details(self, server_id: int) -> dict[str, Any]:
        """Get details for a specific server."""
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server/{server_id}", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def reset_password(self, server_id: int) -> int:
        """Reset server root password.

        Returns task_id for polling the result.
        """
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/server/{server_id}/resetpassword", headers=headers
            )
            resp.raise_for_status()
            result = resp.json()
            return result["task_id"]

    async def get_task_result(self, server_id: int, task_id: int) -> dict[str, Any]:
        """Get task status and result.

        Returns dict with keys:
        - name: task name
        - activated: ISO timestamp
        - assigned: ISO timestamp or empty string
        - completed: ISO timestamp or empty string (if not done)
        - results: result string or empty string (if not done)
        """
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/server/{server_id}/task/{task_id}", headers=headers
            )
            resp.raise_for_status()
            return resp.json()

    async def wait_for_password_reset(
        self, server_id: int, task_id: int, timeout: int = 300, poll_interval: int = 5
    ) -> str:
        """Poll task until complete and extract new password.

        Args:
            server_id: Server ID
            task_id: Task ID from reset_password
            timeout: Maximum wait time in seconds
            poll_interval: Polling interval in seconds

        Returns:
            New root password

        Raises:
            TimeoutError: If task doesn't complete within timeout
            ValueError: If password not found in results
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Password reset task {task_id} did not complete within {timeout}s"
                )

            task = await self.get_task_result(server_id, task_id)

            if task.get("completed"):
                # Task completed, extract password from results
                results = task.get("results", "")
                password = self._extract_password(results)
                if password:
                    logger.info(f"Password reset completed for server {server_id}")
                    return password
                else:
                    raise ValueError(f"Password not found in task results: {results}")

            # Not done yet, wait and retry
            await asyncio.sleep(poll_interval)

    def _extract_password(self, results: str) -> str | None:
        """Extract password from task results string.

        Expected format: "New password: Xk9$mP3qR7"
        or variations like "Password: ...", "Root password: ...", etc.
        """
        # Try various password patterns
        patterns = [
            r"(?:New\s+)?[Pp]assword:\s*(\S+)",
            r"(?:Root\s+)?[Pp]assword:\s*(\S+)",
            r"(?:New\s+)?root\s+password:\s*(\S+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, results, re.IGNORECASE)
            if match:
                return match.group(1)

        return None
