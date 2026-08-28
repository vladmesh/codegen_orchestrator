"""The non-destructive BitLaunch route for the run-owned e2e target."""

from __future__ import annotations

import os
from typing import Protocol

import httpx

BITLAUNCH_PROVIDER = "bitlaunch"
_REQUIRED_LABELS = {"contour": "stand", "stand_role": "target"}


class ServerIdentity(Protocol):
    provider: str | None
    provider_id: str | None
    is_managed: bool
    labels: dict


def parse_bitlaunch_server_id(value: str | int | None) -> str | None:
    """Validate BitLaunch's public positive-decimal server identity."""
    if value is None:
        return None
    normalized = str(value)
    if not normalized.isascii() or not normalized.isdecimal() or int(normalized) <= 0:
        return None
    return normalized


def authorize_run_owned_target(server: ServerIdentity, *, run_tag: str | None) -> str | None:
    """Authorize only this run's exact BitLaunch target, never a general fleet."""
    labels = server.labels if isinstance(server.labels, dict) else {}
    provider_id = parse_bitlaunch_server_id(server.provider_id)
    if (
        server.provider != BITLAUNCH_PROVIDER
        or not server.is_managed
        or provider_id is None
        or labels.get("provider") != BITLAUNCH_PROVIDER
        or labels.get("provider_id") != provider_id
        or not run_tag
        or labels.get("stand_run_tag") != run_tag
        or any(labels.get(key) != value for key, value in _REQUIRED_LABELS.items())
    ):
        return None
    return provider_id


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
