"""Fail-closed policy for Time4VPS servers the orchestrator may manage."""

import os
from typing import Protocol

_ENV_NAME = "TIME4VPS_MANAGED_SERVER_IDS"


class ProvisionableServer(Protocol):
    """Minimal server shape required by the provisioning policy."""

    is_managed: bool

    @property
    def provider_id(self) -> str | None: ...


def parse_time4vps_server_id(value: int | str | None) -> int | None:
    """Normalize one positive ASCII-decimal provider ID."""
    if value is None:
        return None
    normalized = str(value)
    if not normalized.isascii() or not normalized.isdecimal():
        return None
    server_id = int(normalized)
    return server_id if server_id > 0 else None


def managed_time4vps_server_ids() -> frozenset[int]:
    """Return explicitly managed provider IDs; missing configuration manages nothing."""
    raw = os.getenv(_ENV_NAME)
    if raw is None or not raw.strip():
        return frozenset()

    parts = [part.strip() for part in raw.split(",")]
    parsed = [parse_time4vps_server_id(part) for part in parts]
    if any(server_id is None for server_id in parsed):
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers")
    return frozenset(server_id for server_id in parsed if server_id is not None)


def time4vps_server_is_allowed(server_id: int | str | None) -> bool:
    """Return whether destructive management is explicitly allowed for a provider ID."""
    normalized = parse_time4vps_server_id(server_id)
    return normalized is not None and normalized in managed_time4vps_server_ids()


def authorized_time4vps_server_id(server: ProvisionableServer) -> int | None:
    """Return the authorized provider ID, or None when the server fails policy."""
    server_id = parse_time4vps_server_id(server.provider_id)
    if not server.is_managed or server_id not in managed_time4vps_server_ids():
        return None
    return server_id


def server_is_provisioning_allowed(server: ProvisionableServer) -> bool:
    """Apply the complete DB-record and provider-ID authorization policy."""
    return authorized_time4vps_server_id(server) is not None


def provider_ip_matches(*, expected_ip: str, provider_ip: str | None) -> bool:
    """Require a provider IP and an exact match to the authorized database target."""
    return bool(provider_ip) and provider_ip == expected_ip
