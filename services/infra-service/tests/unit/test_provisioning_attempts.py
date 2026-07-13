from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.provisioner.node import ProvisionerNode


def _server(attempts: int = 0):
    return SimpleNamespace(
        public_ip="203.0.113.10",
        host="203.0.113.10",
        status="pending_setup",
        os_template=None,
        provisioning_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_exhausted_reservation_prevents_ansible_and_returns_terminal_result(monkeypatch):
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server(3)))
    reserve_attempt = AsyncMock(return_value=None)
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)
    monkeypatch.setattr("src.provisioner.node.create_incident", AsyncMock())

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {"status": "failed", "reason": "max_attempts_exhausted"}
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    update_status.assert_awaited_once_with("srv-1", "error")
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_first_reserved_attempt_is_passed_to_provisioning_path(monkeypatch):
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(return_value=(1, "episode-1"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    monkeypatch.setattr(
        node,
        "_init_time4vps_client",
        AsyncMock(return_value=(MagicMock(), 1, None)),
    )
    monkeypatch.setattr(node, "_should_reinstall", MagicMock(return_value=False))
    existing_path = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
    monkeypatch.setattr(node, "_run_existing_access_path", existing_path)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["status"] == "success"
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    assert existing_path.await_args.kwargs["provisioning_attempts"] == 1
    assert existing_path.await_args.kwargs["provisioning_episode_id"] == "episode-1"


@pytest.mark.asyncio
async def test_reservation_api_error_prevents_ansible_without_fallback(monkeypatch):
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(side_effect=RuntimeError("api down"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "attempt_reservation_failed",
    }
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    update_status.assert_awaited_once_with("srv-1", "error")
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_success_resets_attempts_before_marking_server_ready(monkeypatch):
    from src.provisioner.handlers import handle_provisioning_success

    calls = []

    async def _reset(server_handle, attempt_number, episode_id):
        calls.append(("reset", server_handle, attempt_number, episode_id))
        return True

    async def _status(server_handle, status):
        calls.append(("status", server_handle, status))

    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", _reset)
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", _status)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins", AsyncMock())

    await handle_provisioning_success("srv-1", "203.0.113.10", 1, "episode-1", False)

    assert calls == [("reset", "srv-1", 1, "episode-1"), ("status", "srv-1", "ready")]
