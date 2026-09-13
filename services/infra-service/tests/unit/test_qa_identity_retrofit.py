"""The step that reconciles one managed target to the current QA target profile.

Hosts provisioned before the QA account existed, or before the wrapper learned a
verb, are recorded complete and still cannot serve QA. This is the repair, and
what these tests hold it to is its order: the stored key is parsed, the
administrative login and the privilege path are each proved by their own run,
the role is applied and proved, and only then is a receipt written. Every
earlier failure is one typed verdict with its exact phase, carrying the
connection identity it was found over, and none of them writes a label or a
receipt.
"""

from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import ServerDTO, TargetIdentity, TargetReadinessPhase
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.provisioner.api_client import TargetReadinessSupersededError
from src.provisioner.operations import (
    NOT_RECONCILABLE,
    QA_IDENTITY_RETROFIT_PLAYBOOK,
    TARGET_READINESS_LOGIN_PLAYBOOK,
    TARGET_READINESS_PRIVILEGE_PLAYBOOK,
    retrofit_qa_identity,
)

FLEET_KEY = fleet_private_key()
FLEET_FINGERPRINT = normalize_admin_private_key(FLEET_KEY).fingerprint
PROOF_OUTPUT = (
    'ok: [203.0.113.10] => {"msg": {"qa_identity_proof": "qa-identity-proof: qa-observer '
    f'uid=1001 login=ok qa_target_version={QA_TARGET_PROFILE_VERSION}"}}}}\nPLAY RECAP ok=14'
)
REVISION = "a" * 40
# What `AnsibleRunner` returns when a run is killed at its timeout: no recap, no
# task line, nothing that says how far the play got.
TIMED_OUT = "Timeout after 180s"


def _server(**overrides) -> ServerDTO:
    """A host provisioned by the Ansible that predates the current QA profile."""
    base = {
        "handle": "vps-1001",
        "host": "vps-1001.example.test",
        "public_ip": "203.0.113.10",
        "ssh_user": "root",
        "status": "active",
        "provider": "time4vps",
        "provider_id": "1001",
        "is_managed": True,
        "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


PROVED_IDENTITY = TargetIdentity(
    ssh_user="root",
    host="vps-1001.example.test",
    public_ip="203.0.113.10",
    ssh_key_fingerprint=FLEET_FINGERPRINT,
)


class Target:
    """The reconciliation's world: one server, one key, a playbook runner and the API."""

    def __init__(self) -> None:
        self.runner = MagicMock()
        self.answers = {
            TARGET_READINESS_LOGIN_PLAYBOOK: (True, "PLAY RECAP ok=1"),
            TARGET_READINESS_PRIVILEGE_PLAYBOOK: (True, "PLAY RECAP ok=2"),
            QA_IDENTITY_RETROFIT_PLAYBOOK: (True, PROOF_OUTPUT),
        }
        self.runner.run_playbook.side_effect = lambda **call: self.answers[call["playbook_name"]]
        self.label = AsyncMock()
        self.report = AsyncMock()

    @property
    def playbooks(self) -> list[str]:
        return [call.kwargs["playbook_name"] for call in self.runner.run_playbook.call_args_list]

    @property
    def verdict(self):
        return self.report.await_args.args[1]


@pytest.fixture
def target():
    world = Target()
    with (
        patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=_server())),
        patch(
            "src.provisioner.operations.get_server_ssh_key",
            new=AsyncMock(return_value=FLEET_KEY),
        ),
        patch("src.provisioner.operations.record_qa_identity", new=world.label),
        patch("src.provisioner.operations.report_target_readiness", new=world.report),
    ):
        yield world


class TestTheRepair:
    async def test_it_runs_the_identity_playbook_as_the_administrative_account(self, target):
        success, message = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is True
        assert "vps-1001" in message
        call = target.runner.run_playbook.call_args.kwargs
        assert call["playbook_name"] == "qa_identity_retrofit.yml"
        # The account the fleet key opens is both how Ansible gets in and whose
        # home the target-local QA agent left its files in.
        assert call["ssh_user"] == "root"
        assert call["deploy_user"] == "root"
        assert call["ssh_private_key"] == FLEET_KEY
        # The account and the profile are the role's own defaults, never a caller's.
        assert not call.get("extra_vars")
        target.label.assert_awaited_once_with("vps-1001")

    async def test_login_then_privilege_are_each_proved_before_anything_changes(self, target):
        await retrofit_qa_identity("vps-1001", target.runner)

        assert target.playbooks == [
            TARGET_READINESS_LOGIN_PLAYBOOK,
            TARGET_READINESS_PRIVILEGE_PLAYBOOK,
            QA_IDENTITY_RETROFIT_PLAYBOOK,
        ]

    async def test_success_records_the_exact_proved_profile_revision_and_identity(self, target):
        await retrofit_qa_identity("vps-1001", target.runner, revision=REVISION)

        handle, verdict = target.report.await_args.args
        assert handle == "vps-1001"
        assert verdict.ready is True
        assert verdict.profile_version == QA_TARGET_PROFILE_VERSION
        assert verdict.proved_at is not None
        assert verdict.revision == REVISION
        assert verdict.identity == PROVED_IDENTITY

    async def test_a_failed_playbook_leaves_the_row_saying_the_host_has_no_identity(self, target):
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (
            False,
            "TASK [Refuse an account of this name that this role did not create] failed",
        )

        success, message = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert "QA identity retrofit failed" in message
        target.label.assert_not_awaited()
        assert target.verdict.ready is False
        assert target.verdict.profile_version is None
        assert target.verdict.identity == PROVED_IDENTITY

    async def test_a_host_the_role_refuses_is_journalled_against_its_handle(self, target):
        """The role stops at an account of that name it did not create."""
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (
            False,
            "fatal: qa-observer already exists on this host and was not created by this role",
        )

        success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        target.label.assert_not_awaited()
        handle, verdict = target.report.await_args.args
        assert handle == "vps-1001"
        assert verdict.phase is TargetReadinessPhase.QA_IDENTITY_ROLE
        assert "already exists on this host" in verdict.detail

    async def test_a_proof_that_fails_is_its_own_phase(self, target):
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (
            False,
            "qa-identity-proof: qa-observer reaches a /usr/local/bin/qa-docker that is not "
            f"QA target profile {QA_TARGET_PROFILE_VERSION}",
        )

        success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert target.verdict.phase is TargetReadinessPhase.QA_IDENTITY_PROOF
        target.label.assert_not_awaited()

    async def test_a_play_that_proved_another_profile_writes_no_receipt(self, target):
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (
            True,
            PROOF_OUTPUT.replace(QA_TARGET_PROFILE_VERSION, "0123456789abcdef"),
        )

        success, message = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert target.verdict.phase is TargetReadinessPhase.QA_IDENTITY_PROOF
        assert "0123456789abcdef" in message
        target.label.assert_not_awaited()

    async def test_a_repeat_is_another_noop_run_and_another_receipt(self, target):
        """Every step is a state, so running it twice is running it once."""
        first = await retrofit_qa_identity("vps-1001", target.runner)
        second = await retrofit_qa_identity("vps-1001", target.runner)

        assert first == second
        assert target.playbooks.count(QA_IDENTITY_RETROFIT_PLAYBOOK) == 2
        assert target.label.await_count == 2
        assert [call.args[1].ready for call in target.report.await_args_list] == [True, True]

    async def test_a_partial_failure_resumes_on_the_next_run(self, target):
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (False, "TASK [Install acl] failed")
        await retrofit_qa_identity("vps-1001", target.runner)
        target.answers[QA_IDENTITY_RETROFIT_PLAYBOOK] = (True, PROOF_OUTPUT)

        success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is True
        assert [call.args[1].ready for call in target.report.await_args_list] == [False, True]

    async def test_a_verdict_the_api_refuses_as_superseded_is_not_a_result(self, target):
        """The row's identity changed while the probes ran: nothing may be claimed for it."""
        target.report.side_effect = TargetReadinessSupersededError("vps-1001: identity changed")

        with pytest.raises(TargetReadinessSupersededError):
            await retrofit_qa_identity("vps-1001", target.runner)

    async def test_the_label_it_writes_is_the_one_the_runtime_reads(self):
        """The row must end up saying exactly what the QA runtime looks for."""
        from src.provisioner.api_client import record_qa_identity

        with patch("src.provisioner.api_client.update_server_labels", new=AsyncMock()) as labels:
            await record_qa_identity("vps-1001")

        assert labels.await_args.args[1] == {QA_SSH_USER_LABEL: QA_SSH_USER}


class TestTheKeyTheLoginAndThePrivilegePathAreSeparateEvidence:
    async def test_a_host_with_no_stored_key_is_journalled_and_not_touched(self, target):
        with patch(
            "src.provisioner.operations.get_server_ssh_key", new=AsyncMock(return_value=None)
        ):
            success, message = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert message == "Server has no stored SSH key"
        target.runner.run_playbook.assert_not_called()
        assert target.verdict.phase is TargetReadinessPhase.SSH_KEY_MISSING
        assert target.verdict.identity.ssh_key_fingerprint is None

    async def test_a_stored_key_that_does_not_parse_is_journalled_without_its_material(
        self, target
    ):
        lines = FLEET_KEY.splitlines()
        truncated = "\n".join([lines[0], lines[1][:24], lines[-1]]) + "\n"
        with patch(
            "src.provisioner.operations.get_server_ssh_key",
            new=AsyncMock(return_value=truncated),
        ):
            success, message = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        target.runner.run_playbook.assert_not_called()
        assert target.verdict.phase is TargetReadinessPhase.SSH_KEY_INVALID
        assert lines[1][:24] not in target.verdict.detail
        assert lines[1][:24] not in message

    @pytest.mark.parametrize(
        "output",
        [
            'fatal: [203.0.113.10]: UNREACHABLE! => {"msg": "Permission denied (publickey)."}',
            TIMED_OUT,
            "an ansible failure with no recognisable words at all",
        ],
        ids=["refused", "timed_out", "unrecognised"],
    )
    async def test_any_failure_of_the_login_run_is_admin_login(self, target, output):
        target.answers[TARGET_READINESS_LOGIN_PLAYBOOK] = (False, output)

        success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert target.playbooks == [TARGET_READINESS_LOGIN_PLAYBOOK]
        assert target.verdict.phase is TargetReadinessPhase.ADMIN_LOGIN
        target.label.assert_not_awaited()

    @pytest.mark.parametrize(
        "output",
        [
            'fatal: [203.0.113.10]: FAILED! => {"msg": "Missing sudo password"}',
            TIMED_OUT,
        ],
        ids=["password_prompt", "timed_out"],
    )
    async def test_any_failure_of_the_privilege_run_is_privilege_preflight(self, target, output):
        """The login already succeeded in its own run, so this failure cannot be the login."""
        target.answers[TARGET_READINESS_PRIVILEGE_PLAYBOOK] = (False, output)

        success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        assert target.playbooks == [
            TARGET_READINESS_LOGIN_PLAYBOOK,
            TARGET_READINESS_PRIVILEGE_PLAYBOOK,
        ]
        assert target.verdict.phase is TargetReadinessPhase.PRIVILEGE_PREFLIGHT


class TestTheFreshPathRecordsTheIdentityWithThePhase:
    @pytest.fixture
    def fresh(self):
        with (
            patch("src.provisioner.api_client.update_server_labels", new=AsyncMock()) as labels,
            patch(
                "src.provisioner.api_client.get_server_info",
                new=AsyncMock(return_value=_server()),
            ),
            patch(
                "src.provisioner.api_client.get_server_ssh_key",
                new=AsyncMock(return_value=FLEET_KEY),
            ) as key,
            patch("src.provisioner.api_client.report_target_readiness", new=AsyncMock()) as report,
        ):
            yield labels, key, report

    async def test_completion_writes_the_phase_and_the_identity_in_one_call(self, fresh):
        """One write, so a host cannot read as provisioned and lend no identity."""
        from src.provisioner.api_client import mark_provisioning_complete

        labels, _, _ = fresh
        await mark_provisioning_complete("vps-1001", PROOF_OUTPUT)

        assert labels.await_args.args[1] == {
            PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE,
            QA_SSH_USER_LABEL: QA_SSH_USER,
        }

    async def test_completion_records_the_receipt_the_software_play_proved(self, fresh):
        from src.provisioner.api_client import mark_provisioning_complete

        _, _, report = fresh
        await mark_provisioning_complete("vps-1001", PROOF_OUTPUT)

        verdict = report.await_args.args[1]
        assert verdict.profile_version == QA_TARGET_PROFILE_VERSION
        assert verdict.identity == PROVED_IDENTITY

    async def test_a_software_play_without_a_current_proof_leaves_no_receipt(self, fresh):
        from src.provisioner.api_client import mark_provisioning_complete

        _, _, report = fresh
        await mark_provisioning_complete("vps-1001", "PLAY RECAP ok=40")

        report.assert_not_awaited()

    async def test_a_row_whose_key_is_not_stored_yet_is_left_unproved(self, fresh):
        from src.provisioner.api_client import mark_provisioning_complete

        _, key, report = fresh
        key.return_value = None
        await mark_provisioning_complete("vps-1001", PROOF_OUTPUT)

        report.assert_not_awaited()


class TestItRefusesAHostItCannotRepair:
    async def test_an_unmanaged_host_is_not_touched(self):
        runner = MagicMock()
        with (
            patch(
                "src.provisioner.operations.get_server_info",
                new=AsyncMock(return_value=_server(is_managed=False)),
            ),
            patch("src.provisioner.operations.report_target_readiness", new=AsyncMock()) as report,
        ):
            success, message = await retrofit_qa_identity("vps-1001", runner)

        assert success is False
        assert message == NOT_RECONCILABLE
        runner.run_playbook.assert_not_called()
        report.assert_not_awaited()

    async def test_a_host_still_being_provisioned_belongs_to_the_provisioner(self, target):
        with patch(
            "src.provisioner.operations.get_server_info",
            new=AsyncMock(return_value=_server(status="provisioning")),
        ):
            success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is False
        target.runner.run_playbook.assert_not_called()
        target.report.assert_not_awaited()

    @pytest.mark.parametrize("status", ["error", "unreachable", "reserved"])
    async def test_a_phase_complete_row_in_a_non_admitting_status_is_reconciled(
        self, target, status
    ):
        """Its lifecycle status is the API's to preserve; the proof still runs."""
        with patch(
            "src.provisioner.operations.get_server_info",
            new=AsyncMock(return_value=_server(status=status)),
        ):
            success, _ = await retrofit_qa_identity("vps-1001", target.runner)

        assert success is True
        assert target.verdict.ready is True

    async def test_a_prepared_manual_target_needs_no_provider_authority(self, target, monkeypatch):
        """No provider id, no allowlist entry: explicit management is the authority."""
        monkeypatch.delenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", raising=False)
        manual = _server(
            handle="prod-target-5wwb",
            provider=None,
            provider_id=None,
            labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        )
        with patch(
            "src.provisioner.operations.get_server_info", new=AsyncMock(return_value=manual)
        ):
            success, _ = await retrofit_qa_identity("prod-target-5wwb", target.runner)

        assert success is True
        assert target.verdict.ready is True
