"""Provisioning attempt fences reach every route and contain stale success."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.dto.server import TargetIdentity
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION, QATargetProof
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.provisioner.node import ProvisionerNode

GENERATED_KEY = fleet_private_key()
PROOF = QATargetProof(
    profile_version=QA_TARGET_PROFILE_VERSION,
    proved_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    ssh_user="dev",
    ssh_key_fingerprint=normalize_admin_private_key(GENERATED_KEY).fingerprint,
)


def _server(attempts: int = 0, status: str = "pending_setup") -> SimpleNamespace:
    return SimpleNamespace(
        public_ip="203.0.113.10",
        host="203.0.113.10",
        status=status,
        ssh_user="dev",
        ssh_key_fingerprint=None,
        os_template=None,
        provisioning_attempts=attempts,
        is_managed=True,
        provider="time4vps",
        provider_id="1001",
        labels={"provider": "time4vps", "provider_id": "1001"},
    )


@pytest.mark.asyncio
async def test_exhausted_reservation_prevents_ansible(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server(3)))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt", AsyncMock(return_value=None)
    )
    status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", status)
    monkeypatch.setattr("src.provisioner.node.create_incident", AsyncMock())

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["reason"] == "max_attempts_exhausted"
    status.assert_awaited_once_with("srv-1", "error")
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_reservation_api_error_prevents_ansible_without_fallback(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve = AsyncMock(side_effect=RuntimeError("api down"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve)
    status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["reason"] == "attempt_reservation_failed"
    reserve.assert_awaited_once_with("srv-1", 3)
    status.assert_awaited_once_with("srv-1", "error")
    node.ansible_runner.run_playbook.assert_not_called()


@pytest.mark.asyncio
async def test_missing_provider_credentials_consumes_the_reserved_attempt(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    monkeypatch.delenv("TIME4VPS_LOGIN", raising=False)
    monkeypatch.delenv("TIME4VPS_USERNAME", raising=False)
    monkeypatch.delenv("TIME4VPS_PASSWORD", raising=False)
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_server()))
    reserve = AsyncMock(return_value=(1, "episode-1"))
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve)
    status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["reason"] == "time4vps_credentials_missing"
    reserve.assert_awaited_once()
    status.assert_awaited_once_with("srv-1", "error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason", "marks_error"),
    [
        ("unmanaged", "server_not_authorized", False),
        ("missing_ip", "server_ip_missing", True),
        ("unlisted", "server_not_authorized", False),
    ],
    ids=["unmanaged", "missing_ip", "unlisted"],
)
async def test_unauthorized_targets_are_refused_before_reservation(
    monkeypatch, mode, reason, marks_error
):
    server = _server()
    if mode == "missing_ip":
        monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
        server.public_ip = None
        server.host = ""
    elif mode == "unlisted":
        monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "2002")
    else:
        server.is_managed = False
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=server))
    reserve = AsyncMock()
    status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve)
    monkeypatch.setattr("src.provisioner.node.update_server_status", status)

    result = await node.run({"server_to_provision": "srv-1", "errors": []})

    assert result["provisioning_result"]["reason"] == reason
    reserve.assert_not_awaited()
    if marks_error:
        status.assert_awaited_once_with("srv-1", "error")
    else:
        status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "route"),
    [("pending_setup", "_run_existing_access_path"), ("force_rebuild", "_run_reinstall_path")],
)
async def test_every_provisioning_route_receives_the_reserved_fence_and_identity(
    monkeypatch, status, route
):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    server = _server(status=status)
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=server))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "episode-1")),
    )
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    monkeypatch.setattr(node, "_init_time4vps_client", AsyncMock(return_value=MagicMock()))
    selected = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
    monkeypatch.setattr(node, route, selected)

    await node.run({"server_to_provision": "srv-1", "errors": []})

    call = selected.await_args.kwargs
    assert (call["provisioning_attempts"], call["provisioning_episode_id"]) == (1, "episode-1")
    assert call["expected_identity"] == TargetIdentity(
        ssh_user="dev",
        host="203.0.113.10",
        public_ip="203.0.113.10",
        ssh_key_fingerprint=None,
    )


@pytest.mark.asyncio
async def test_finalizer_conflict_is_superseded_with_incident_but_no_operator_row_write(
    monkeypatch,
):
    from shared.contracts.dto.server import ProvisioningFinalizationDisposition
    from src.provisioner.handlers import handle_provisioning_success

    manager = MagicMock()
    manager.get_private_key.return_value = GENERATED_KEY
    monkeypatch.setattr(
        "src.provisioner.handlers.finalize_provisioning",
        AsyncMock(return_value=ProvisioningFinalizationDisposition.CONFLICT),
    )
    status = AsyncMock()
    incident = AsyncMock()
    monkeypatch.setattr("src.provisioner.handlers.update_server_status", status)
    monkeypatch.setattr("src.provisioner.handlers.create_incident", incident)

    result = await handle_provisioning_success(
        "srv-1",
        "203.0.113.10",
        1,
        "episode-1",
        False,
        ssh_manager=manager,
        qa_target_proof=PROOF,
        expected_identity=TargetIdentity(
            ssh_user="dev",
            host="203.0.113.10",
            public_ip="203.0.113.10",
            ssh_key_fingerprint=None,
        ),
        retain_finalization=AsyncMock(),
    )

    assert result["provisioning_result"]["status"] == "superseded"
    status.assert_not_awaited()
    incident.assert_awaited_once()
    assert incident.await_args.args[2]["reason"] == "finalization_conflict"
