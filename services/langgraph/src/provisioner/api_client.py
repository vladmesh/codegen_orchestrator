"""API client for provisioner - communicates with the API service."""

from http import HTTPStatus
import os

import httpx

from shared.logging_config import get_logger

logger = get_logger(__name__)


def _get_api_url() -> str:
    """Get API base URL."""
    return os.getenv("API_URL", "http://api:8000")


async def get_server_info(server_handle: str) -> dict | None:
    """Fetch server info from API.

    Args:
        server_handle: Server handle

    Returns:
        Server info dict or None on error
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/api/servers/{server_handle}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(
            "api_server_info_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return None


async def update_server_status(server_handle: str, status: str) -> bool:
    """Update server status in database via API.

    Args:
        server_handle: Server handle
        status: New status

    Returns:
        True if successful
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{api_url}/api/servers/{server_handle}", json={"status": status}
            )
            resp.raise_for_status()
            logger.info(
                "api_server_status_updated",
                server_handle=server_handle,
                status=status,
            )
            return True
    except Exception as e:
        logger.error(
            "api_server_status_update_failed",
            server_handle=server_handle,
            status=status,
            error=str(e),
        )
        return False


async def update_server_labels(server_handle: str, labels: dict) -> bool:
    """Update server labels in database via API.

    Args:
        server_handle: Server handle
        labels: New labels dict (will be merged with existing)

    Returns:
        True if successful
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            # Fetch current to merge safely
            resp = await client.get(f"{api_url}/api/servers/{server_handle}")
            if resp.status_code == HTTPStatus.OK:
                current_labels = resp.json().get("labels", {}) or {}
                current_labels.update(labels)
                final_labels = current_labels
            else:
                final_labels = labels

            resp = await client.patch(
                f"{api_url}/api/servers/{server_handle}", json={"labels": final_labels}
            )
            resp.raise_for_status()
            logger.info(
                "api_server_labels_updated",
                server_handle=server_handle,
                labels=final_labels,
            )
            return True
    except Exception as e:
        logger.error(
            "api_server_labels_update_failed",
            server_handle=server_handle,
            labels=labels,
            error=str(e),
        )
        return False


async def get_services_on_server(server_handle: str) -> list[dict]:
    """Get services deployed on a server for redeployment.

    Args:
        server_handle: Server handle

    Returns:
        List of service deployment records
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/api/servers/{server_handle}/services")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(
            "api_server_services_fetch_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return []


async def increment_provisioning_attempts(server_handle: str) -> bool:
    """Increment provisioning attempts counter for a server.

    Args:
        server_handle: Server handle

    Returns:
        True if successful
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            # Get current count
            resp = await client.get(f"{api_url}/api/servers/{server_handle}")
            if resp.status_code != HTTPStatus.OK:
                return False

            current = resp.json().get("provisioning_attempts", 0)

            # Increment
            resp = await client.patch(
                f"{api_url}/api/servers/{server_handle}",
                json={"provisioning_attempts": current + 1},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(
            "api_provisioning_attempt_increment_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return False
