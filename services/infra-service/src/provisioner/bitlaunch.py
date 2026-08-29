"""The non-destructive BitLaunch route for the run-owned e2e target."""

from __future__ import annotations

import os

import httpx

from shared.provisioning_policy import (
    BITLAUNCH_PROVIDER,
    authorize_run_owned_target,
)

__all__ = ["BITLAUNCH_PROVIDER", "BitLaunchClient", "authorize_run_owned_target"]


class BitLaunchClient:
    """Read-only provider observation used before the existing-SSH route."""

    api_root = "https://app.bitlaunch.io/api"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("BITLAUNCH_API_KEY is required")
        self._api_key = api_key

    @classmethod
    def from_environment(cls) -> BitLaunchClient:
        api_key = os.getenv("BITLAUNCH_API_KEY")
        if not api_key:
            raise ValueError("BITLAUNCH_API_KEY is required")
        return cls(api_key)

    async def get_server_ip(self, provider_id: str) -> str | None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.api_root}/servers/{provider_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        payload = response.json()
        server = payload.get("server", payload) if isinstance(payload, dict) else None
        if not isinstance(server, dict):
            return None
        ip = server.get("ipv4")
        return ip if isinstance(ip, str) and ip else None
