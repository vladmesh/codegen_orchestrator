"""Time4VPS Client for Internal API."""

import asyncio
import base64
import os
import re
from typing import Any

import httpx

from shared.logging_config import get_logger
from shared.schemas import Time4VPSServer, Time4VPSServerDetails, Time4VPSTask

logger = get_logger(__name__)


class Time4VPSClient:
    """Client for Time4VPS API."""

    def __init__(self, username: str | None = None, password: str | None = None):
        """Initialize Time4VPS Client.

        Args:
            username: Time4VPS username. Defaults to TIME4VPS_USERNAME env var.
            password: Time4VPS password. Defaults to TIME4VPS_PASSWORD env var.
        """
        self.base_url = "https://billing.time4vps.com/api"
        self.username = username or os.getenv("TIME4VPS_USERNAME")
        self.password = password or os.getenv("TIME4VPS_PASSWORD")
        self._auth_header: str | None = None

        if not self.username or not self.password:
            logger.warning(
                "time4vps_credentials_missing",
                username_set=bool(self.username),
                password_set=bool(self.password),
            )

    def _get_auth_header(self) -> dict[str, str]:
        """Construct Basic Auth header."""
        if not self._auth_header:
            if not self.username or not self.password:
                raise ValueError("Time4VPS credentials not set (username/password)")

            auth_str = f"{self.username}:{self.password}"
            encoded_auth = base64.b64encode(auth_str.encode()).decode()
            self._auth_header = f"Basic {encoded_auth}"
        return {"Authorization": self._auth_header}

    async def get_servers(self) -> list[Time4VPSServer]:
        """List all servers."""
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # API returns list of servers
            return [Time4VPSServer.model_validate(item) for item in data]

    async def get_server_details(self, server_id: int) -> Time4VPSServerDetails:
        """Get details for a specific server.

        Note: The response does not include server_id - use the parameter if needed.
        """
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server/{server_id}", headers=headers)
            resp.raise_for_status()
            return Time4VPSServerDetails.model_validate(resp.json())

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
                logger.error("time4vps_reset_password_missing_task_id", response=result)
                raise ValueError(f"No task_id in reset_password response: {result}")

            return result["task_id"]

    async def get_task_result(self, server_id: int, task_id: int) -> Time4VPSTask:
        """Get task status and result."""
        headers = self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/server/{server_id}/task/{task_id}", headers=headers
            )
            resp.raise_for_status()
            return Time4VPSTask.model_validate(resp.json())

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
        start_time = asyncio.get_running_loop().time()

        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Password reset task {task_id} did not complete within {timeout}s"
                )

            task = await self.get_task_result(server_id, task_id)

            if task.completed:
                # Task completed, extract password from results
                results = task.results or ""
                logger.debug("time4vps_password_reset_results", results=results)
                password = self.extract_password(results)
                if password:
                    logger.info(
                        "time4vps_password_reset_completed",
                        server_id=server_id,
                        password_length=len(password),
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

        logger.info(
            "time4vps_reinstall_triggered",
            server_id=server_id,
            os_template=os_template,
        )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/server/{server_id}/reinstall", headers=headers, json=payload
            )
            resp.raise_for_status()
            result = resp.json()
            logger.debug("time4vps_reinstall_response", response=result)

            if "task_id" not in result:
                logger.error("time4vps_reinstall_missing_task_id", response=result)
                raise ValueError(f"No task_id in reinstall response: {result}")

            task_id = result["task_id"]
            logger.info("time4vps_reinstall_task_created", task_id=task_id, server_id=server_id)
            return task_id

    async def get_server_id_by_handle(self, server_handle: str) -> int | None:
        """Get Time4VPS server_id from handle.

        Args:
            server_handle: Server handle (e.g., 'vps-267179')

        Returns:
            server_id if found, None otherwise
        """
        try:
            servers = await self.get_servers()
            logger.info("time4vps_servers_listed", count=len(servers))

            for server in servers:
                # server is now Time4VPSServer model
                srv_id = server.id
                if not srv_id:
                    logger.warning("time4vps_server_missing_id", server=server.model_dump())
                    continue

                # Match by handle (vps-{id})
                if f"vps-{srv_id}" == server_handle:
                    return srv_id

            logger.error(
                "time4vps_server_handle_not_found",
                server_handle=server_handle,
                scanned=len(servers),
            )
            return None
        except Exception:
            logger.exception("time4vps_server_lookup_failed", server_handle=server_handle)
            return None

    async def wait_for_task(
        self, server_id: int, task_id: int, timeout: int = 600, poll_interval: int = 10
    ) -> Time4VPSTask:
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
        start_time = asyncio.get_running_loop().time()

        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

            task = await self.get_task_result(server_id, task_id)

            if task.completed:
                logger.info("time4vps_task_completed", server_id=server_id, task_id=task_id)
                return task

            logger.debug(
                "time4vps_task_waiting",
                server_id=server_id,
                task_id=task_id,
                poll_interval=poll_interval,
            )
            await asyncio.sleep(poll_interval)
