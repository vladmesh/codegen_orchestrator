"""Time4VPS Client for Internal API."""

import base64
import httpx
from typing import Any

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
