"""Time4VPS Client for Internal API."""

import asyncio
import base64
import json
import os
import re
from typing import Any

import httpx

from shared.log_config import get_logger
from shared.schemas import Time4VPSServer, Time4VPSServerDetails, Time4VPSTask

logger = get_logger(__name__)

# Provider error bodies are short JSON like {"error":["ipnotallowed","unauthorized"]}.
# Cap anyway so an HTML error page can't flood the log or an exception message.
_ERROR_BODY_LIMIT = 2000

# The billing API throttles consecutive actions on one server and refuses the extra
# call with 401 — the very status it also uses for a real authorization failure.
# Only this key inside the error body tells the two apart.
_RATE_LIMIT_KEY = "wait_x_between_action"


class Time4VPSAPIError(Exception):
    """Time4VPS answered with 4xx/5xx. Carries the response body, which names the reason.

    The provider answers with the actual cause (`ipnotallowed`, `wronglogin`), while
    httpx's own message only says "Unauthorized". Keep the body on the exception so
    callers log the reason without re-issuing the request by hand.
    """

    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {url} -> {status_code}: {body}")

    @property
    def rate_limit_wait_seconds(self) -> int | None:
        """Seconds the billing API asks us to wait, or None if this is not a rate limit.

        The observed refusal is 401 with
        ``{"error":[["wait_x_between_action",24],"unauthorized"]}``. The status code
        carries no information here — a genuine loss of authorization answers 401 with
        the same shape minus this key — so the body is what classifies the response.
        Anything else, including a plain 401, returns None and stays fatal.
        """
        if _RATE_LIMIT_KEY not in self.body:
            return None
        # The key is present, so the body must be the documented rate-limit shape.
        # If it is not, the provider changed its contract: crash instead of guessing.
        for entry in json.loads(self.body)["error"]:
            if isinstance(entry, list) and entry[0] == _RATE_LIMIT_KEY:
                return int(entry[1])
        raise ValueError(f"Unrecognized Time4VPS rate-limit body: {self.body}")


class Time4VPSClient:
    """Client for Time4VPS API."""

    def __init__(self, username: str | None = None, password: str | None = None):
        """Initialize Time4VPS Client.

        Args:
            username: Time4VPS account login. Callers pass it explicitly; the env
                fallback is TIME4VPS_USERNAME.
            password: Time4VPS password. Defaults to TIME4VPS_PASSWORD env var.

        Note: access to the API is restricted by an IP allowlist on the provider side.
        A correct login from an unlisted address answers 401 with
        {"error":["ipnotallowed","unauthorized"]}.
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send an authenticated request, logging the response body on 4xx/5xx."""
        headers = self._get_auth_header()
        url = f"{self.base_url}{path}"

        async with httpx.AsyncClient() as client:
            resp = await client.request(method, url, headers=headers, **kwargs)

        if resp.is_error:
            body = resp.text[:_ERROR_BODY_LIMIT]
            logger.error(
                "time4vps_http_error",
                method=method,
                url=url,
                status_code=resp.status_code,
                body=body,
            )
            raise Time4VPSAPIError(method, url, resp.status_code, body)

        return resp

    async def get_servers(self) -> list[Time4VPSServer]:
        """List all servers."""
        resp = await self._request("GET", "/server")
        # API returns list of servers
        return [Time4VPSServer.model_validate(item) for item in resp.json()]

    async def get_server_details(self, server_id: int) -> Time4VPSServerDetails:
        """Get details for a specific server.

        Note: The response does not include server_id - use the parameter if needed.
        """
        resp = await self._request("GET", f"/server/{server_id}")
        return Time4VPSServerDetails.model_validate(resp.json())

    async def reset_password(self, server_id: int) -> int:
        """Reset server root password.

        Returns task_id for polling the result.
        """
        resp = await self._request("POST", f"/server/{server_id}/resetpassword")
        result = resp.json()

        if "task_id" not in result:
            logger.error("time4vps_reset_password_missing_task_id", response=result)
            raise ValueError(f"No task_id in reset_password response: {result}")

        return result["task_id"]

    async def get_task_result(self, server_id: int, task_id: int) -> Time4VPSTask:
        """Get task status and result."""
        resp = await self._request("GET", f"/server/{server_id}/task/{task_id}")
        return Time4VPSTask.model_validate(resp.json())

    async def wait_for_password_reset(
        self, server_id: int, task_id: int, timeout: int = 300, poll_interval: int = 5
    ) -> str:
        """Wait for a password reset task and extract the new password from its result.

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
        task = await self.wait_for_task(
            server_id, task_id, timeout=timeout, poll_interval=poll_interval
        )

        results = task.results or ""
        logger.debug("time4vps_password_reset_results", results=results)
        password = self.extract_password(results)
        if not password:
            raise ValueError(f"Password not found in task results: {results}")

        logger.info(
            "time4vps_password_reset_completed",
            server_id=server_id,
            password_length=len(password),
        )
        return password

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
        resp = await self._request("GET", f"/server/{server_id}/oses")
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

        resp = await self._request("POST", f"/server/{server_id}/reinstall", json=payload)
        result = resp.json()
        logger.debug("time4vps_reinstall_response", response=result)

        if "task_id" not in result:
            logger.error("time4vps_reinstall_missing_task_id", response=result)
            raise ValueError(f"No task_id in reinstall response: {result}")

        task_id = result["task_id"]
        logger.info("time4vps_reinstall_task_created", task_id=task_id, server_id=server_id)
        return task_id

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
            Time4VPSAPIError: For any provider error other than the billing rate limit.
                The rate limit is the only transient poll answer: the provider states
                it explicitly, names the interval to wait, and the task it refuses to
                report on keeps running regardless. Every other error stands for an
                unknown state and still ends the wait.
        """
        start_time = asyncio.get_running_loop().time()

        while True:
            remaining = timeout - (asyncio.get_running_loop().time() - start_time)
            if remaining <= 0:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

            try:
                task = await self.get_task_result(server_id, task_id)
            except Time4VPSAPIError as exc:
                rate_limit_wait = exc.rate_limit_wait_seconds
                if rate_limit_wait is None:
                    raise
                # Waiting the interval the provider asked for, but never past the
                # caller's budget: a rate limit extends the poll, it cannot outlive it.
                logger.warning(
                    "time4vps_task_poll_rate_limited",
                    server_id=server_id,
                    task_id=task_id,
                    wait_seconds=rate_limit_wait,
                    remaining_seconds=round(remaining),
                )
                await asyncio.sleep(min(rate_limit_wait, remaining))
                continue

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
