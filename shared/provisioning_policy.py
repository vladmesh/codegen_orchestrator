"""Fail-closed policy for Time4VPS servers the orchestrator may manage."""

import os

_ENV_NAME = "TIME4VPS_MANAGED_SERVER_IDS"


def managed_time4vps_server_ids() -> frozenset[int]:
    """Return explicitly managed provider IDs; missing configuration manages nothing."""
    raw = os.getenv(_ENV_NAME)
    if raw is None or not raw.strip():
        return frozenset()

    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers")
    try:
        server_ids = frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers") from exc

    if any(server_id <= 0 for server_id in server_ids):
        raise ValueError(f"{_ENV_NAME} must contain comma-separated positive integers")
    return server_ids


def time4vps_server_is_allowed(server_id: int) -> bool:
    """Return whether destructive management is explicitly allowed for a provider ID."""
    return server_id in managed_time4vps_server_ids()
