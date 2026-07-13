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
async def test_success_marks_server_ready_before_resolving_incident_journal(monkeypatch):
    from src.provisioner.handlers import handle_provisioning_success

    calls = []

    async def _reset(server_handle, attempt_number, episode_id):
        calls.append(("reset", server_handle, attempt_number, episode_id))
        return True

    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", _reset)
    update_status = AsyncMock(return_value=True)
    resolve_incidents = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", update_status)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", resolve_incidents)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins", AsyncMock())

    await handle_provisioning_success("srv-1", "203.0.113.10", 1, "episode-1", False)

    assert calls == [("reset", "srv-1", 1, "episode-1")]
    update_status.assert_awaited_once_with("srv-1", "ready")
    resolve_incidents.assert_awaited_once_with("srv-1")


@pytest.mark.asyncio
async def test_success_keeps_server_ready_when_incident_journal_is_unavailable(monkeypatch):
    from src.provisioner.handlers import handle_provisioning_success

    monkeypatch.setattr(
        "src.provisioner.handlers.reset_provisioning_attempts", AsyncMock(return_value=True)
    )
    update_status = AsyncMock(return_value=True)
    notify = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", update_status)
    monkeypatch.setattr(
        "src.provisioner.handlers.resolve_active_incidents",
        AsyncMock(side_effect=RuntimeError("api unavailable")),
    )
    monkeypatch.setattr("src.provisioner.handlers.notify_admins", notify)

    result = await handle_provisioning_success("srv-1", "203.0.113.10", 1, "episode-1", False)

    update_status.assert_awaited_once_with("srv-1", "ready")
    assert result["provisioning_result"]["status"] == "success"
    assert result["provisioning_result"]["incident_journal_status"] == "pending_reconciliation"
    assert "incident journal could not be closed" in result["messages"][0]["message"]
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_success_result_survives_notification_api_failure(monkeypatch):
    """A best-effort notification cannot turn a READY server into a failed result."""
    from src.provisioner.handlers import handle_provisioning_success

    monkeypatch.setattr(
        "src.provisioner.handlers.reset_provisioning_attempts", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", AsyncMock())
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", AsyncMock())
    monkeypatch.setattr(
        "src.provisioner.handlers.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    result = await handle_provisioning_success("srv-1", "203.0.113.10", 1, "episode-1", False)

    assert result["provisioning_result"]["status"] == "success"


@pytest.mark.asyncio
async def test_reinstall_progress_notification_is_best_effort(monkeypatch):
    from src.provisioner.operations import _notify_admins_best_effort

    monkeypatch.setattr(
        "src.provisioner.operations.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    await _notify_admins_best_effort("reinstall started", "info", "srv-1")


@pytest.mark.asyncio
async def test_recovery_notification_is_best_effort(monkeypatch):
    from src.provisioner.recovery import _notify_admins_best_effort

    monkeypatch.setattr(
        "src.provisioner.recovery.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    await _notify_admins_best_effort("redeployment complete", "success", "srv-1")


@pytest.mark.asyncio
async def test_stale_success_skips_ready_status_and_all_success_side_effects(monkeypatch):
    from src.provisioner.handlers import handle_provisioning_success

    monkeypatch.setattr(
        "src.provisioner.handlers.reset_provisioning_attempts", AsyncMock(return_value=False)
    )
    update_status = AsyncMock()
    save_key = AsyncMock()
    resolve_incidents = AsyncMock()
    redeploy = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(
        "src.provisioner.handlers.update_server_status", update_status, raising=False
    )
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", save_key)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", resolve_incidents)
    monkeypatch.setattr("src.provisioner.handlers.redeploy_all_services", redeploy)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins", notify)

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", True, ssh_manager=MagicMock()
    )

    assert result["provisioning_result"]["status"] == "superseded"
    update_status.assert_not_awaited()
    save_key.assert_not_awaited()
    resolve_incidents.assert_not_awaited()
    redeploy.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_success_maps_to_superseded_result_not_failure(monkeypatch):
    """Stale success must reach the result stream as first-class SUPERSEDED.

    Chain: handle_provisioning_success (reset=False) -> node result ->
    process_provisioner_job. A superseded completion must not be misread as a
    failure, otherwise the scheduler would flip an actively-provisioning server
    to UNREACHABLE and raise a false alarm.
    """
    from shared.contracts.queues.provisioner import ProvisionerResult
    from shared.contracts.vocab import ResultStatus
    from src.main import process_provisioner_job
    from src.provisioner.handlers import handle_provisioning_success

    # A newer episode already owns the server, so the conditional reset misses.
    monkeypatch.setattr(
        "src.provisioner.handlers.reset_provisioning_attempts", AsyncMock(return_value=False)
    )
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", AsyncMock(), raising=False)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins", AsyncMock())

    stale_state = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-old", False
    )

    async def _run(self, state):
        return stale_state

    monkeypatch.setattr("src.main.ProvisionerNode.run", _run)

    result = await process_provisioner_job({"job_id": "job-1", "server_handle": "srv-1"})

    assert result.status == ResultStatus.SUPERSEDED
    assert result.status != ResultStatus.FAILED
    assert result.errors is None
    # The published wire form round-trips as a valid contract for consumers.
    wire = ProvisionerResult.model_validate(result.model_dump(mode="json"))
    assert wire.status == ResultStatus.SUPERSEDED
