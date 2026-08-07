from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.provisioner.node import ProvisionerNode


def _ssh_manager(private_key: str | None = "PRIVATE-KEY"):
    """SSHManager stub holding the container-local private key."""
    manager = MagicMock()
    manager.get_private_key.return_value = private_key
    return manager


def _server(attempts: int = 0, status: str = "pending_setup"):
    return SimpleNamespace(
        public_ip="203.0.113.10",
        host="203.0.113.10",
        status=status,
        ssh_user="dev",
        os_template=None,
        provisioning_attempts=attempts,
        is_managed=True,
        provider_id="1001",
        labels={"provider_id": "1001"},
    )


@pytest.mark.asyncio
async def test_exhausted_reservation_prevents_ansible_and_returns_terminal_result(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server(3)))
    reserve_attempt = AsyncMock(return_value=None)
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)
    monkeypatch.setattr("src.provisioner.node.create_incident", AsyncMock())
    init_client = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(node, "_init_time4vps_client", init_client)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {"status": "failed", "reason": "max_attempts_exhausted"}
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    update_status.assert_awaited_once_with("srv-1", "error")
    init_client.assert_not_awaited()
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_first_attempt_uses_existing_access_without_force_rebuild_status(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(return_value=(1, "episode-1"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    monkeypatch.setattr(
        node,
        "_init_time4vps_client",
        AsyncMock(return_value=MagicMock()),
    )
    existing_path = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
    monkeypatch.setattr(node, "_run_existing_access_path", existing_path)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["status"] == "success"
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    assert existing_path.await_args.kwargs["provisioning_attempts"] == 1
    assert existing_path.await_args.kwargs["provisioning_episode_id"] == "episode-1"
    assert existing_path.await_args.kwargs["deploy_user"] == "dev"


@pytest.mark.asyncio
async def test_force_rebuild_status_selects_reinstall_path(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    server = _server(status="force_rebuild")
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=server))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "episode-1")),
    )
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)
    monkeypatch.setattr(node, "_init_time4vps_client", AsyncMock(return_value=MagicMock()))
    reinstall_path = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
    existing_path = AsyncMock()
    monkeypatch.setattr(node, "_run_reinstall_path", reinstall_path)
    monkeypatch.setattr(node, "_run_existing_access_path", existing_path)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["status"] == "success"
    update_status.assert_awaited_once_with("srv-1", "provisioning")
    reinstall_path.assert_awaited_once()
    assert reinstall_path.await_args.kwargs["server_id"] == 1001
    existing_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_reservation_api_error_prevents_ansible_without_fallback(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(side_effect=RuntimeError("api down"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)
    init_client = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(node, "_init_time4vps_client", init_client)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "attempt_reservation_failed",
    }
    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    update_status.assert_awaited_once_with("srv-1", "error")
    init_client.assert_not_awaited()
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_missing_provider_credentials_consume_reserved_attempt(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    monkeypatch.delenv("TIME4VPS_LOGIN", raising=False)
    monkeypatch.delenv("TIME4VPS_USERNAME", raising=False)
    monkeypatch.delenv("TIME4VPS_PASSWORD", raising=False)
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(return_value=(1, "episode-1"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    reserve_attempt.assert_awaited_once_with("srv-1", 3)
    update_status.assert_awaited_once_with("srv-1", "error")
    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "time4vps_credentials_missing",
    }
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_server_is_rejected_before_attempt_reservation(monkeypatch):
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    unmanaged = _server()
    unmanaged.is_managed = False
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=unmanaged))
    reserve_attempt = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "server_not_authorized",
    }
    reserve_attempt.assert_not_awaited()
    update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_server_ip_is_rejected_and_marked_error_before_reservation(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    missing_ip = _server()
    missing_ip.public_ip = None
    missing_ip.host = ""
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=missing_ip))
    reserve_attempt = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve_attempt)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "server_ip_missing",
    }
    reserve_attempt.assert_not_awaited()
    update_status.assert_awaited_once_with("srv-1", "error")


@pytest.mark.asyncio
async def test_unlisted_server_is_rejected_before_any_state_write(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "2002")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve_attempt = AsyncMock(return_value=(1, "episode-1"))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        reserve_attempt,
    )
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)
    monkeypatch.setattr(
        node,
        "_init_time4vps_client",
        AsyncMock(return_value=MagicMock()),
    )
    existing_path = AsyncMock()
    reinstall_path = AsyncMock()
    monkeypatch.setattr(node, "_run_existing_access_path", existing_path)
    monkeypatch.setattr(node, "_run_reinstall_path", reinstall_path)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "server_not_authorized",
    }
    reserve_attempt.assert_not_awaited()
    update_status.assert_not_awaited()
    existing_path.assert_not_awaited()
    reinstall_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_persists_ssh_key_before_closing_episode_and_journal(monkeypatch):
    """Success is committed key-first, then episode close (which writes READY).

    Previously this test asserted the handler's own ``update_server_status(...,
    "ready")`` call. That call is gone: the terminal status is now written
    atomically by the reset endpoint, together with the attempt-counter reset and
    only while the episode is current, so the handler must not write it a second
    time from the outside.
    """
    from src.provisioner.handlers import handle_provisioning_success

    calls = []

    async def _save_key(server_handle, key):
        calls.append(("save_key", server_handle, key))

    async def _reset(server_handle, attempt_number, episode_id):
        calls.append(("reset", server_handle, attempt_number, episode_id))
        return True

    async def _resolve(server_handle):
        calls.append(("resolve", server_handle))

    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", _save_key)
    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", _reset)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", _resolve)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", AsyncMock())

    result = await handle_provisioning_success(
        "srv-1",
        "203.0.113.10",
        1,
        "episode-1",
        False,
        ssh_manager=_ssh_manager("PRIVATE-KEY"),
    )

    assert calls == [
        ("save_key", "srv-1", "PRIVATE-KEY"),
        ("reset", "srv-1", 1, "episode-1"),
        ("resolve", "srv-1"),
    ]
    assert result["provisioning_result"]["status"] == "success"
    # No second, unconditional status writer is left in the success path.
    from src.provisioner import handlers

    assert not hasattr(handlers, "update_server_status")


@pytest.mark.asyncio
async def test_success_keeps_server_ready_when_incident_journal_is_unavailable(monkeypatch):
    """Unchanged contract, minus the removed handler-owned status write."""
    from src.provisioner.handlers import handle_provisioning_success

    reset = AsyncMock(return_value=True)
    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", reset)
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(
        "src.provisioner.handlers.resolve_active_incidents",
        AsyncMock(side_effect=RuntimeError("api unavailable")),
    )
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", notify)

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", False, ssh_manager=_ssh_manager()
    )

    # The episode close is what marks the server READY.
    reset.assert_awaited_once_with("srv-1", 1, "episode-1")
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
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", AsyncMock())
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", AsyncMock())
    monkeypatch.setattr(
        "shared.notifications.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", False, ssh_manager=_ssh_manager()
    )

    assert result["provisioning_result"]["status"] == "success"


@pytest.mark.asyncio
async def test_reinstall_progress_notification_is_best_effort(monkeypatch):
    from shared.notifications import notify_admins_best_effort

    monkeypatch.setattr(
        "shared.notifications.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    await notify_admins_best_effort("reinstall started", "info", server_handle="srv-1")


@pytest.mark.asyncio
async def test_recovery_notification_is_best_effort(monkeypatch):
    from shared.notifications import notify_admins_best_effort

    monkeypatch.setattr(
        "shared.notifications.notify_admins",
        AsyncMock(side_effect=RuntimeError("users API down")),
    )

    await notify_admins_best_effort("redeployment complete", "success", server_handle="srv-1")


@pytest.mark.asyncio
async def test_existing_access_success_stores_key_resets_attempts_and_marks_ready(monkeypatch):
    """The whole existing-access path, from green playbooks to a fixed success.

    Fails without the fix on the ordering: the handler used to reset attempts
    first and could return before the key was ever stored.
    """
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    ansible = MagicMock()
    ansible.run_playbook.return_value = (True, "ok")
    node = ProvisionerNode(ssh_manager=_ssh_manager(), ansible_runner=ansible)
    node.ssh_manager.get_public_key.return_value = "PUBLIC-KEY"

    server = _server()
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=server))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "episode-1")),
    )
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    monkeypatch.setattr("src.provisioner.node.update_server_labels", AsyncMock())
    monkeypatch.setattr(node, "_init_time4vps_client", AsyncMock(return_value=MagicMock()))

    # The DB stand-in the API endpoints write through.
    db = {"ssh_key": None, "attempts": 1, "episode_id": "episode-1", "status": "provisioning"}

    async def _save_key(server_handle, key):
        db["ssh_key"] = key

    async def _reset(server_handle, attempt_number, episode_id):
        if (db["attempts"], db["episode_id"]) != (attempt_number, episode_id):
            return False
        db.update(attempts=0, episode_id=None, status="ready")
        return True

    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", _save_key)
    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", _reset)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", AsyncMock())
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", AsyncMock())

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["status"] == "success"
    assert db == {"ssh_key": "PRIVATE-KEY", "attempts": 0, "episode_id": None, "status": "ready"}


@pytest.mark.asyncio
async def test_ssh_key_save_failure_is_not_a_success_and_leaves_the_episode_open(monkeypatch):
    """A rejected key write fails provisioning instead of declaring READY."""
    from src.provisioner.handlers import handle_provisioning_success

    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.provisioner.handlers.save_server_ssh_key",
        AsyncMock(side_effect=RuntimeError("API rejected the key")),
    )
    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", reset)
    resolve_incidents = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", resolve_incidents)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", AsyncMock())

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", False, ssh_manager=_ssh_manager()
    )

    assert result["provisioning_result"]["status"] == "failed"
    assert result["provisioning_result"]["reason"] == "save_server_ssh_key_failed"
    assert result["errors"]
    # Nothing marks the server READY behind a key that is not in the DB.
    reset.assert_not_awaited()
    resolve_incidents.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ssh_manager", "reason"),
    [(None, "ssh_manager_missing"), (_ssh_manager(None), "ssh_private_key_missing")],
)
async def test_unusable_ssh_manager_is_not_a_success(monkeypatch, ssh_manager, reason):
    """No key source means failed provisioning, not a silently skipped save."""
    from src.provisioner.handlers import handle_provisioning_success

    reset = AsyncMock(return_value=True)
    save_key = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", save_key)
    monkeypatch.setattr("src.provisioner.handlers.reset_provisioning_attempts", reset)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", AsyncMock())
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", AsyncMock())

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", False, ssh_manager=ssh_manager
    )

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": reason,
        "server_handle": "srv-1",
        "server_ip": "203.0.113.10",
    }
    save_key.assert_not_awaited()
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_success_keeps_the_key_but_skips_all_other_success_side_effects(monkeypatch):
    """A superseded attempt still stores the key of the server it provisioned.

    This test used to assert ``save_key.assert_not_awaited()``. That is exactly
    the defect this card fixes: the server was really provisioned with this
    container's key, and dropping the key on the superseded branch loses access
    to it for good. Everything else stays a no-op — the newer episode owns the
    status, the incident journal and the redeployment.
    """
    from src.provisioner.handlers import handle_provisioning_success

    monkeypatch.setattr(
        "src.provisioner.handlers.reset_provisioning_attempts", AsyncMock(return_value=False)
    )
    save_key = AsyncMock()
    resolve_incidents = AsyncMock()
    redeploy = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", save_key)
    monkeypatch.setattr("src.provisioner.handlers.resolve_active_incidents", resolve_incidents)
    monkeypatch.setattr("src.provisioner.handlers.redeploy_all_services", redeploy)
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", notify)

    result = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-1", True, ssh_manager=_ssh_manager()
    )

    assert result["provisioning_result"]["status"] == "superseded"
    save_key.assert_awaited_once_with("srv-1", "PRIVATE-KEY")
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
    monkeypatch.setattr("src.provisioner.handlers.save_server_ssh_key", AsyncMock())
    monkeypatch.setattr("src.provisioner.handlers.notify_admins_best_effort", AsyncMock())

    stale_state = await handle_provisioning_success(
        "srv-1", "203.0.113.10", 1, "episode-old", False, ssh_manager=_ssh_manager()
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
