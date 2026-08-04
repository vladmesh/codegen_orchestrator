"""Tests for the startup re-publication of provisioning triggers.

These go through the real HTTP stack (respx intercepts at the transport level),
so a request that reaches the internal API without `X-Internal-Key` fails here
exactly like it fails on the live stack.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from shared.contracts.dto.server import ServerStatus

API_BASE_URL = "http://127.0.0.1:9"
SERVERS_URL = f"{API_BASE_URL}/api/servers/"


def _server_row(handle: str = "vps-pending", *, is_managed: bool = True) -> dict:
    return {
        "id": 1,
        "handle": handle,
        "host": "pending.example.com",
        "public_ip": "203.0.113.7",
        "ssh_user": "root",
        "status": ServerStatus.PENDING_SETUP.value,
        "is_managed": is_managed,
        "labels": {},
        "provisioning_attempts": 0,
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:00Z",
    }


def _authorized(request: httpx.Request) -> bool:
    return request.headers.get("X-Internal-Key") == os.environ["INTERNAL_API_KEY"]


@pytest.fixture
def api_client_reset():
    """Drop the cached httpx client so each test gets a fresh event loop binding."""
    from src.clients.api import api_client

    api_client._client = None
    yield api_client
    api_client._client = None


@pytest.fixture
def internal_api(api_client_reset):
    """Internal API that answers 401 to any request without the internal key."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not _authorized(request):
            return httpx.Response(401, json={"detail": "Unauthorized"})
        if request.url.params.get("status") != ServerStatus.PENDING_SETUP.value:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[_server_row()])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(SERVERS_URL).mock(side_effect=handler)
        yield mock


async def test_pending_server_gets_a_trigger(internal_api):
    from src.tasks import provisioner_trigger

    with patch.object(
        provisioner_trigger, "publish_provisioner_trigger", new_callable=AsyncMock
    ) as publish:
        await provisioner_trigger.retry_pending_servers()

    publish.assert_awaited_once_with("vps-pending", is_incident_recovery=False)


async def test_pending_unmanaged_server_does_not_get_startup_trigger(api_client_reset):
    def handler(request: httpx.Request) -> httpx.Response:
        assert _authorized(request)
        return httpx.Response(200, json=[_server_row(is_managed=False)])

    with (
        respx.mock(assert_all_called=False) as mock,
        patch(
            "src.tasks.provisioner_trigger.publish_provisioner_trigger", new_callable=AsyncMock
        ) as publish,
    ):
        mock.get(SERVERS_URL).mock(side_effect=handler)
        from src.tasks import provisioner_trigger

        await provisioner_trigger.retry_pending_servers()

    publish.assert_not_awaited()


async def test_wrong_internal_key_fails_loudly(internal_api, monkeypatch):
    """A rejected key must raise, not read as "no pending servers"."""
    from src.tasks import provisioner_trigger

    monkeypatch.setattr(provisioner_trigger.api_client, "_internal_api_key", "not-the-internal-key")

    with patch.object(
        provisioner_trigger, "publish_provisioner_trigger", new_callable=AsyncMock
    ) as publish:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await provisioner_trigger.retry_pending_servers()

    assert exc_info.value.response.status_code == httpx.codes.UNAUTHORIZED
    publish.assert_not_awaited()


def test_missing_internal_key_fails_at_construction(monkeypatch):
    from src.clients.api import SchedulerAPIClient

    monkeypatch.delenv("INTERNAL_API_KEY")

    with pytest.raises(KeyError):
        SchedulerAPIClient()


async def test_publish_failure_propagates():
    """A trigger that was not published must not be reported as published."""
    from src.tasks import provisioner_trigger

    broken_redis = AsyncMock()
    broken_redis.publish.side_effect = ConnectionError("redis is down")

    with patch.object(provisioner_trigger.redis, "from_url", return_value=broken_redis):
        with pytest.raises(ConnectionError):
            await provisioner_trigger.publish_provisioner_trigger("vps-pending")

    broken_redis.close.assert_awaited_once()
