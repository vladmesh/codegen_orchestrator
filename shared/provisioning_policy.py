"""Fail-closed policy for Time4VPS servers the orchestrator may manage."""

import os

from shared.contracts.dto.server import ServerDTO

_ENV_NAME = "TIME4VPS_MANAGED_SERVER_IDS"


def managed_time4vps_server_ids() -> frozenset[int]:
    """Return explicitly managed provider IDs; missing configuration manages nothing."""
    raw = os.getenv(_ENV_NAME)
    if raw is None or not raw.strip():
        return frozenset()

    parts = [part.strip() for part in raw.split(",")]
    if any(not part or not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers")
    server_ids = frozenset(int(part) for part in parts)

    if any(server_id <= 0 for server_id in server_ids):
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers")
    return server_ids


def time4vps_server_is_allowed(server_id: int | str | None) -> bool:
    """Return whether destructive management is explicitly allowed for a provider ID."""
    if server_id is None:
        return False
    normalized = str(server_id)
    if not normalized.isascii() or not normalized.isdecimal():
        return False
    return int(normalized) in managed_time4vps_server_ids()


def server_is_provisioning_allowed(server: ServerDTO) -> bool:
    """Apply the complete DB-record and provider-ID authorization policy."""
    return server.is_managed and time4vps_server_is_allowed(server.provider_id)
