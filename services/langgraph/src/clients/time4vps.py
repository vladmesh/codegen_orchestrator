"""Time4VPS Client."""

import base64
import os
from typing import Any

import httpx


class Time4VPSClient:
    """Client for Time4VPS API."""

    def __init__(self, api_url: str | None = None):
        self.base_url = "https://billing.time4vps.com/api"
        # Internal API URL to fetch credentials
        self.internal_api_url = api_url or os.getenv("API_URL", "http://api:8000")
        self._auth_header: str | None = None

    async def _get_credentials(self) -> dict[str, str]:
        """Fetch credentials from internal API."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.internal_api_url}/api/api-keys/time4vps")
            resp.raise_for_status()
            data = resp.json()
            return data["value"]  # {"username": "...", "password": "..."}

    async def _get_auth_header(self) -> dict[str, str]:
        """Construct Basic Auth header."""
        if not self._auth_header:
            creds = await self._get_credentials()
            username = creds["username"]
            password = creds["password"]
            auth_str = f"{username}:{password}"
            encoded_auth = base64.b64encode(auth_str.encode()).decode()
            self._auth_header = f"Basic {encoded_auth}"
        return {"Authorization": self._auth_header}

    async def get_servers(self) -> list[dict[str, Any]]:
        """List all servers."""
        headers = await self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_server_details(self, server_id: int) -> dict[str, Any]:
        """Get details for a specific server."""
        headers = await self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server/{server_id}", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def order_server(self, product_id: int, domain: str, cycle: str = "m") -> dict[str, Any]:
        """Order a new server."""
        headers = await self._get_auth_header()
        payload = {
            "domain": domain,
            "cycle": cycle,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/order/{product_id}", headers=headers, json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def reinstall_server(self, server_id: int, os_name: str, ssh_key: str) -> dict[str, Any]:
        """Reinstall server with OS and SSH key."""
        headers = await self._get_auth_header()
        payload = {"os": os_name, "ssh_key": ssh_key}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/server/{server_id}/reinstall", headers=headers, json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def get_available_os(self, server_id: int) -> list[dict[str, Any]]:
        """Get available OS list for a server."""
        headers = await self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/server/{server_id}/oses", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_dns_zones(self) -> dict[str, Any]:
        """List DNS zones."""
        headers = await self._get_auth_header()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/dns", headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def add_dns_record(
        self, domain_id: int, name: str, type: str, content: str, ttl: int = 300
    ) -> dict[str, Any]:
        """Add DNS record."""
        headers = await self._get_auth_header()
        payload = {"name": name, "type": type, "content": content, "ttl": ttl}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/dns/{domain_id}/record", headers=headers, json=payload
            )
            resp.raise_for_status()
            return resp.json()
