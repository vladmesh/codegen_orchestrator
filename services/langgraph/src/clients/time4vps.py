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

            if "task_id" not in result:
                logger.error(f"Unexpected reset_password response: {result}")
                raise ValueError(f"No task_id in reset_password response: {result}")

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
                logger.debug(f"Password reset results: {results}")
                password = self.extract_password(results)
                if password:
                    logger.info(
                        f"Password reset completed for server {server_id}, password length: {len(password)}"
                    )
                    return password
                else:
                    raise ValueError(f"Password not found in task results: {results}")

            # Not done yet, wait and retry
            await asyncio.sleep(poll_interval)

    def extract_password(self, results: str) -> str | None:
        """Extract password from task results string.

        Time4VPS returns password in HTML format:
        Password: \t<a onclick='...this.innerHTML = "ACTUAL_PASSWORD"'>Click...</a>

        Also handles plain text format: "New password: Xk9$mP3qR7"
        """
        # First try to extract from HTML format (innerHTML = "password")
        html_pattern = r'innerHTML\s*=\s*["\']([^"\']+)["\']'
        match = re.search(html_pattern, results)
        if match:
            return match.group(1)

        # Fallback to plain text patterns
        patterns = [
            r"(?:New\s+)?[Pp]assword:\s*(\S+)",
            r"(?:Root\s+)?[Pp]assword:\s*(\S+)",
            r"(?:New\s+)?root\s+password:\s*(\S+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, results, re.IGNORECASE)
            if match:
                password = match.group(1)
                # Skip if it's HTML tag
                if password.startswith("<"):
                    continue
                return password

        return None

    async def get_available_os_templates(self, server_id: int) -> list[dict[str, Any]]:
        """Get available OS templates for reinstall.

        Args:
            server_id: Server ID

        Returns:
            List of available OS templates
        """
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server/{server_id}/oses", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def reinstall_server(
        self,
        server_id: int,
        os_template: str,
        ssh_key: str | None = None,
        init_script_id: int | None = None,
    ) -> int:
        """Reinstall server with specified OS.

        WARNING: All data on the server will be lost!

        Args:
            server_id: Server ID
            os_template: OS template name (e.g., "kvm-ubuntu-24.04-gpt-x86_64")
            ssh_key: Optional SSH public key for immediate access
            init_script_id: Optional init script ID

        Returns:
            task_id for polling completion
        """
        headers = self._get_auth_header()
        payload: dict[str, Any] = {"os": os_template}

        if ssh_key:
            payload["ssh_key"] = ssh_key
        if init_script_id:
            payload["script"] = init_script_id

        logger.info(f"Triggering OS reinstall for server {server_id} with template {os_template}")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/server/{server_id}/reinstall", headers=headers, json=payload
            )
            resp.raise_for_status()
            result = resp.json()
            logger.debug(f"Reinstall API response: {result}")

            if "task_id" not in result:
                logger.error(f"Unexpected reinstall response: {result}")
                raise ValueError(f"No task_id in reinstall response: {result}")

            task_id = result["task_id"]
            logger.info(f"Reinstall task created: {task_id}")
            return task_id

    async def wait_for_task(
        self, server_id: int, task_id: int, timeout: int = 600, poll_interval: int = 10
    ) -> dict[str, Any]:
        """Wait for any task to complete.

        Args:
            server_id: Server ID
            task_id: Task ID to wait for
            timeout: Maximum wait time in seconds
            poll_interval: Polling interval in seconds

        Returns:
            Task result dict

        Raises:
            TimeoutError: If task doesn't complete within timeout
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

            task = await self.get_task_result(server_id, task_id)

            if task.get("completed"):
                logger.info(f"Task {task_id} completed for server {server_id}")
                return task

            logger.debug(f"Task {task_id} still running, waiting {poll_interval}s...")
            await asyncio.sleep(poll_interval)
