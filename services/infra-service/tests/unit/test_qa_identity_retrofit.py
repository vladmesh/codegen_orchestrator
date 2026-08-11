"""The step that gives an already-provisioned host the QA identity.

Hosts provisioned before the QA account existed are recorded complete and lend
no identity, so exploratory QA refuses them. This is the repair, and what these
tests hold it to is the order of its two halves: the playbook runs first, and the
server row is only told the account exists after the playbook said it does. A
label written ahead of the playbook would be a row that lies to the QA runtime in
exactly the way the runtime cannot detect.
"""

from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import ServerDTO
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from src.provisioner.operations import retrofit_qa_identity


def _server(**overrides) -> ServerDTO:
    """A host provisioned by the Ansible that predates the QA account."""
    base = {
        "handle": "vps-1001",
        "host": "203.0.113.10",
        "public_ip": "203.0.113.10",
        "ssh_user": "root",
        "status": "active",
        "provider_id": "1001",
        "is_managed": True,
        "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


@pytest.fixture(autouse=True)
def _managed(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")


@pytest.fixture
def target():
    """The provisioner's world: one server, one key, and recording collaborators."""
    runner = MagicMock()
    runner.run_playbook.return_value = (True, "ok")
    with (
        patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=_server())),
        patch(
            "src.provisioner.operations.get_server_ssh_key",
            new=AsyncMock(return_value="fleet-key"),
        ),
        patch("src.provisioner.operations.record_qa_identity", new=AsyncMock()) as label,
        patch("src.provisioner.operations.resolve_active_incidents", new=AsyncMock()) as incidents,
    ):
        yield runner, label, incidents


class TestTheRepair:
    async def test_it_runs_the_identity_playbook_as_the_administrative_account(self, target):
        runner, label, _ = target

        success, message = await retrofit_qa_identity("vps-1001", runner)

        assert success is True
        assert "vps-1001" in message
        call = runner.run_playbook.call_args.kwargs
        assert call["playbook_name"] == "qa_identity_retrofit.yml"
        # The account the fleet key opens is both how Ansible gets in and whose
        # home the target-local QA agent left its files in.
        assert call["ssh_user"] == "root"
        assert call["deploy_user"] == "root"
        assert call["ssh_private_key"] == "fleet-key"
        label.assert_awaited_once_with("vps-1001")

    async def test_a_failed_playbook_leaves_the_row_saying_the_host_has_no_identity(self, target):
        runner, label, incidents = target
        runner.run_playbook.return_value = (
            False,
            "TASK [Create the QA observation account] failed",
        )

        success, message = await retrofit_qa_identity("vps-1001", runner)

        assert success is False
        assert "QA identity retrofit failed" in message
        label.assert_not_awaited()
        incidents.assert_not_awaited()

    async def test_a_repeat_is_another_noop_run_and_another_label_write(self, target):
        """Both halves are states, so running it twice is running it once."""
        runner, label, _ = target

        first = await retrofit_qa_identity("vps-1001", runner)
        second = await retrofit_qa_identity("vps-1001", runner)

        assert first == second
        assert runner.run_playbook.call_count == 2
        assert label.await_count == 2

    async def test_the_repair_closes_the_provisioning_failure_it_repairs(self, target):
        """The QA runtime journals the missing identity; a repair closes it."""
        _, _, incidents = target

        await retrofit_qa_identity(
            "vps-1001", MagicMock(**{"run_playbook.return_value": (True, "")})
        )

        incidents.assert_awaited_once_with("vps-1001")

    async def test_the_label_it_writes_is_the_one_the_runtime_reads(self):
        """The row must end up saying exactly what the QA runtime looks for."""
        from src.provisioner.api_client import record_qa_identity

        with patch("src.provisioner.api_client.update_server_labels", new=AsyncMock()) as labels:
            await record_qa_identity("vps-1001")

        assert labels.await_args.args[1] == {QA_SSH_USER_LABEL: QA_SSH_USER}


class TestTheFreshPathRecordsTheIdentityWithThePhase:
    async def test_completion_writes_the_phase_and_the_identity_in_one_call(self):
        """One write, so a host cannot read as provisioned and lend no identity."""
        from src.provisioner.api_client import mark_provisioning_complete

        with patch("src.provisioner.api_client.update_server_labels", new=AsyncMock()) as labels:
            await mark_provisioning_complete("vps-1001")

        assert labels.await_args.args[1] == {
            PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE,
            QA_SSH_USER_LABEL: QA_SSH_USER,
        }


class TestItRefusesAHostItCannotRepair:
    async def test_an_unmanaged_host_is_not_touched(self):
        runner = MagicMock()
        with patch(
            "src.provisioner.operations.get_server_info",
            new=AsyncMock(return_value=_server(is_managed=False)),
        ):
            success, message = await retrofit_qa_identity("vps-1001", runner)

        assert success is False
        assert message == "Server is not authorized for provisioning"
        runner.run_playbook.assert_not_called()

    async def test_a_host_with_no_stored_key_is_not_touched(self):
        runner = MagicMock()
        with (
            patch(
                "src.provisioner.operations.get_server_info", new=AsyncMock(return_value=_server())
            ),
            patch(
                "src.provisioner.operations.get_server_ssh_key", new=AsyncMock(return_value=None)
            ),
        ):
            success, message = await retrofit_qa_identity("vps-1001", runner)

        assert success is False
        assert message == "Server has no stored SSH key"
        runner.run_playbook.assert_not_called()
