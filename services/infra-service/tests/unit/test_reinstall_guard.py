from unittest.mock import AsyncMock, MagicMock

import pytest

from src.provisioner.operations import reinstall_and_provision


@pytest.mark.asyncio
async def test_reinstall_refuses_server_outside_allowlist(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "2002")
    client = MagicMock()
    client.reinstall_server = AsyncMock()

    success, message = await reinstall_and_provision(
        time4vps_client=client,
        server_handle="vps-1001",
        server_id=1001,
        server_ip="203.0.113.10",
        os_template="ubuntu",
        ssh_manager=MagicMock(),
        ansible_runner=MagicMock(),
    )

    assert success is False
    assert "not present in TIME4VPS_MANAGED_SERVER_IDS" in message
    client.reinstall_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_reinstall_refuses_provider_id_with_changed_ip(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    client = MagicMock()
    client.get_server_details = AsyncMock(return_value=MagicMock(ip="203.0.113.99"))
    client.reinstall_server = AsyncMock()

    success, message = await reinstall_and_provision(
        time4vps_client=client,
        server_handle="vps-1001",
        server_id=1001,
        server_ip="203.0.113.10",
        os_template="ubuntu",
        ssh_manager=MagicMock(),
        ansible_runner=MagicMock(),
    )

    assert success is False
    assert "Provider identity mismatch" in message
    client.reinstall_server.assert_not_awaited()
