from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.dto.incident import IncidentType
from shared.provisioning_policy import authorize_run_owned_target
from shared.qa_identity import QA_SSH_USER_LABEL
from src.provisioner.bitlaunch import BitLaunchClient
from src.provisioner.node import ProvisionerNode


def _target(**overrides):
    labels = {
        "contour": "stand",
        "provider": "bitlaunch",
        "provider_id": "6a920e74c9c98a452507b09b",
        "stand_run_tag": "gha-41-1",
        "stand_role": "target",
    }
    labels.update(overrides.pop("labels", {}))
    return SimpleNamespace(
        handle="bitlaunch-6a920e74c9c98a452507b09b",
        public_ip="203.0.113.19",
        host="203.0.113.19",
        status="pending_setup",
        ssh_user="deploy",
        os_template=None,
        is_managed=True,
        provider=overrides.pop("provider", "bitlaunch"),
        provider_id=overrides.pop("provider_id", "6a920e74c9c98a452507b09b"),
        labels=labels,
        **overrides,
    )


@pytest.mark.parametrize(
    ("overrides", "run_tag"),
    [
        ({"provider": "unknown"}, "gha-41-1"),
        ({"provider_id": "not-an-id"}, "gha-41-1"),
        ({"labels": {"stand_role": "orchestrator"}}, "gha-41-1"),
        ({"labels": {"stand_run_tag": "gha-other"}}, "gha-41-1"),
    ],
)
def test_only_the_exact_run_owned_bitlaunch_target_is_authorized(overrides, run_tag):
    assert authorize_run_owned_target(_target(**overrides), run_tag=run_tag) is None


def test_exact_run_owned_bitlaunch_target_is_authorized():
    assert authorize_run_owned_target(_target(), run_tag="gha-41-1") == "6a920e74c9c98a452507b09b"


@pytest.mark.asyncio
async def test_bitlaunch_server_observation_uses_the_server_endpoint_and_envelope(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"server": {"ipv4": "203.0.113.19"}}
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "src.provisioner.bitlaunch.httpx.AsyncClient", MagicMock(return_value=context)
    )

    ip = await BitLaunchClient("provider-token").get_server_ip("6a920e74c9c98a452507b09b")

    assert ip == "203.0.113.19"
    client.get.assert_awaited_once_with(
        "https://app.bitlaunch.io/api/servers/6a920e74c9c98a452507b09b",
        headers={"Authorization": "Bearer provider-token"},
    )
    response.raise_for_status.assert_called_once_with()


@pytest.mark.asyncio
async def test_bitlaunch_checks_provider_binding_before_reserving_or_changing_target(monkeypatch):
    monkeypatch.setenv("STAND_RUN_TAG", "gha-41-1")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_target()))
    binding = AsyncMock(
        side_effect=node._provider_identity_mismatch("bitlaunch-6a920e74c9c98a452507b09b")
    )
    monkeypatch.setattr(node, "_init_bitlaunch_client", binding)
    reserve = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve)
    update_status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", update_status)

    result = await node.run(
        {"server_to_provision": "bitlaunch-6a920e74c9c98a452507b09b", "errors": []}
    )

    assert result["provisioning_result"] == {
        "status": "failed",
        "reason": "provider_identity_mismatch",
    }
    binding.assert_awaited_once()
    reserve.assert_not_awaited()
    update_status.assert_awaited_once_with("bitlaunch-6a920e74c9c98a452507b09b", "error")


@pytest.mark.asyncio
async def test_bitlaunch_ip_mismatch_is_denied_before_ssh_or_reservation(monkeypatch):
    monkeypatch.setenv("STAND_RUN_TAG", "gha-41-1")
    node = ProvisionerNode(ssh_manager=MagicMock(), ansible_runner=MagicMock())
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_target()))
    provider = MagicMock()
    provider.get_server_ip = AsyncMock(return_value="203.0.113.20")
    # Keep the provider observation real, so the test holds the complete stored
    # ID/labels/IP admission instead of mocking its final comparison away.
    monkeypatch.setattr(
        "src.provisioner.node.BitLaunchClient.from_environment", MagicMock(return_value=provider)
    )
    reserve = AsyncMock()
    ssh_key = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.reserve_provisioning_attempt", reserve)
    monkeypatch.setattr("src.provisioner.node.get_server_ssh_key", ssh_key)
    status = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_status", status)

    result = await node.run(
        {"server_to_provision": "bitlaunch-6a920e74c9c98a452507b09b", "errors": []}
    )

    assert result["provisioning_result"]["reason"] == "provider_identity_mismatch"
    reserve.assert_not_awaited()
    ssh_key.assert_not_awaited()
    status.assert_awaited_once_with("bitlaunch-6a920e74c9c98a452507b09b", "error")


@pytest.mark.asyncio
async def test_bitlaunch_uses_stored_creation_key_and_both_existing_ssh_playbooks(monkeypatch):
    monkeypatch.setenv("STAND_RUN_TAG", "gha-41-1")
    ansible = MagicMock()
    ansible.run_playbook.return_value = (True, "ok")
    ssh_manager = MagicMock()
    ssh_manager.get_public_key.return_value = "provisioner-public-key"
    ssh_manager.get_private_key.return_value = "provisioner-private-key"
    node = ProvisionerNode(ssh_manager=ssh_manager, ansible_runner=ansible)
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_target()))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "e1")),
    )
    monkeypatch.setattr(node, "_init_bitlaunch_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "src.provisioner.node.get_server_ssh_key", AsyncMock(return_value="creation-key")
    )
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    labels = AsyncMock()
    complete = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_labels", labels)
    monkeypatch.setattr("src.provisioner.node.mark_provisioning_complete", complete)
    monkeypatch.setattr(
        "src.provisioner.node.handle_provisioning_success",
        AsyncMock(return_value={"provisioning_result": {"status": "success"}}),
    )

    result = await node.run(
        {"server_to_provision": "bitlaunch-6a920e74c9c98a452507b09b", "errors": []}
    )

    assert result["provisioning_result"]["status"] == "success"
    assert [call.kwargs["playbook_name"] for call in ansible.run_playbook.call_args_list] == [
        "provision_access.yml",
        "provision_software.yml",
    ]
    assert ansible.run_playbook.call_args_list[0].kwargs["ssh_user"] == "root"
    assert ansible.run_playbook.call_args_list[0].kwargs["ssh_private_key"] == "creation-key"
    assert ansible.run_playbook.call_args_list[0].kwargs["deploy_user"] == "deploy"
    labels.assert_awaited_once_with(
        "bitlaunch-6a920e74c9c98a452507b09b", {"provisioning_phase": "software_installation"}
    )
    complete.assert_awaited_once_with("bitlaunch-6a920e74c9c98a452507b09b")


@pytest.mark.asyncio
async def test_bitlaunch_stand_profile_reaches_software_playbook_as_an_explicit_flag(monkeypatch):
    monkeypatch.setenv("STAND_RUN_TAG", "gha-41-1")
    ansible = MagicMock()
    ansible.run_playbook.return_value = (True, "ok")
    ssh_manager = MagicMock()
    ssh_manager.get_public_key.return_value = "provisioner-public-key"
    node = ProvisionerNode(ssh_manager=ssh_manager, ansible_runner=ansible)
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_target()))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "e1")),
    )
    monkeypatch.setattr(node, "_init_bitlaunch_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "src.provisioner.node.get_server_ssh_key", AsyncMock(return_value="creation-key")
    )
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    monkeypatch.setattr("src.provisioner.node.update_server_labels", AsyncMock())
    monkeypatch.setattr("src.provisioner.node.mark_provisioning_complete", AsyncMock())
    monkeypatch.setattr(
        "src.provisioner.node.handle_provisioning_success",
        AsyncMock(return_value={"provisioning_result": {"status": "success"}}),
    )

    await node.run(
        {
            "server_to_provision": "bitlaunch-6a920e74c9c98a452507b09b",
            "provisioning_profile": "stand_e2e",
            "errors": [],
        }
    )

    assert ansible.run_playbook.call_args_list[1].kwargs["extra_vars"] == {
        "provisioning_profile": "stand_e2e"
    }


@pytest.mark.asyncio
async def test_a_software_phase_that_failed_records_no_completion_and_no_qa_account(monkeypatch):
    """The whole point of the proof at the end of the software phase.

    A host whose QA identity could not be proved fails that playbook, and a
    failed playbook must leave the server row saying what is true: no
    `provisioning_phase=complete`, no QA-account label, and a provisioning
    incident carrying the playbook output — the same journal entry
    `retrofit_qa_identity` writes when the same role refuses the same host.
    Run 33718999040 met the opposite: a row that said the account was there.
    """
    monkeypatch.setenv("STAND_RUN_TAG", "gha-41-1")
    ansible = MagicMock()
    # Access succeeds, the software phase fails the way a failed proof fails it.
    ansible.run_playbook.side_effect = [
        (True, "ok"),
        (False, "STDOUT TAIL:\nqa-identity-proof: qa-observer has no authorized_keys"),
    ]
    ssh_manager = MagicMock()
    ssh_manager.get_public_key.return_value = "provisioner-public-key"
    node = ProvisionerNode(ssh_manager=ssh_manager, ansible_runner=ansible)
    monkeypatch.setattr("src.provisioner.node.get_server_info", AsyncMock(return_value=_target()))
    monkeypatch.setattr(
        "src.provisioner.node.reserve_provisioning_attempt",
        AsyncMock(return_value=(1, "e1")),
    )
    monkeypatch.setattr(node, "_init_bitlaunch_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        "src.provisioner.node.get_server_ssh_key", AsyncMock(return_value="creation-key")
    )
    monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
    labels = AsyncMock()
    complete = AsyncMock()
    incident = AsyncMock()
    monkeypatch.setattr("src.provisioner.node.update_server_labels", labels)
    monkeypatch.setattr("src.provisioner.node.mark_provisioning_complete", complete)
    monkeypatch.setattr("src.provisioner.node.create_incident", incident)

    result = await node.run(
        {"server_to_provision": "bitlaunch-6a9905aed77393390012fc3e", "errors": []}
    )

    complete.assert_not_awaited()
    written = [call.args[1] for call in labels.await_args_list]
    assert written == [{"provisioning_phase": "software_installation"}]
    assert not any(QA_SSH_USER_LABEL in labels_written for labels_written in written)
    incident.assert_awaited_once()
    journalled = incident.await_args.args
    assert journalled[1] is IncidentType.PROVISIONING_FAILED
    assert "authorized_keys" in journalled[2]["output"]
    assert "Phase 2 failed" in result["errors"]
