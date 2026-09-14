"""A successful provisioning records its QA target proof together with READY.

The software play proves the current profile before the key it generated is
stored. What these tests hold is the order that makes that proof usable and the
refusal to publish it any other way: the key is validated and persisted, the
complete phase is written, the proof is bound to the just-persisted identity,
and the receipt and READY land together — or the server is left `error` with a
provisioning incident and no receipt.
"""

from datetime import UTC, datetime
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import (
    ProvisioningFinalizationDisposition,
    ServerDTO,
    target_identity,
)
from shared.qa_target_profile import (
    QA_TARGET_PROFILE_VERSION,
    QATargetProof,
    current_profile_proof,
)
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.provisioner import handlers
from src.provisioner.node import ProvisionerNode
from src.provisioner.operations import ReinstallOutcome

GENERATED_KEY = fleet_private_key()
GENERATED_FINGERPRINT = normalize_admin_private_key(GENERATED_KEY).fingerprint
PROOF = QATargetProof(
    profile_version=QA_TARGET_PROFILE_VERSION,
    proved_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    ssh_user="root",
    ssh_key_fingerprint=GENERATED_FINGERPRINT,
)
PROOF_OUTPUT = (
    '"qa_identity_proof": "qa-identity-proof: qa-observer uid=1001 login=ok '
    f'qa_target_version={QA_TARGET_PROFILE_VERSION}"\nPLAY RECAP ok=40'
)


def _manager(private_key: str | None = GENERATED_KEY) -> MagicMock:
    manager = MagicMock()
    manager.get_private_key.return_value = private_key
    manager.get_public_key.return_value = "ssh-ed25519 AAAA generated"
    return manager


def _row(**overrides) -> ServerDTO:
    base = {
        "handle": "vps-9",
        "host": "vps-9.example.test",
        "public_ip": "203.0.113.9",
        "ssh_user": "root",
        "status": "provisioning",
        "is_managed": True,
        "labels": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


class Provisioner:
    """The one finalization call the success handler is allowed to make."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []
        self.finalizations: list = []
        self.row = {
            "key": None,
            "complete": False,
            "status": "provisioning",
            "receipt": None,
            "episode": ("episode-1", 1),
            "incidents": [],
        }
        self.finalize_error: Exception | None = None
        self.disposition = ProvisioningFinalizationDisposition.FINALIZED

        async def finalize(handle, request):
            self.calls.append("finalize")
            self.finalizations.append(request)
            if self.finalize_error:
                raise self.finalize_error
            if self.disposition is ProvisioningFinalizationDisposition.IDEMPOTENT:
                return self.disposition
            if request.episode_id != self.row["episode"][0]:
                return ProvisioningFinalizationDisposition.CONFLICT
            if self.disposition is ProvisioningFinalizationDisposition.FINALIZED:
                self.row.update(
                    key=request.generated_private_key,
                    complete=True,
                    status="ready",
                    receipt=request.qa_target_receipt,
                    episode=None,
                )
            return self.disposition

        async def status(handle, value):
            self.calls.append(f"status:{value}")
            self.row["status"] = value

        async def incident(handle, incident_type, details):
            self.row["incidents"].append((incident_type, details))

        for name, fake in {
            "finalize_provisioning": finalize,
            "update_server_status": status,
            "create_incident": incident,
            "notify_admins_best_effort": AsyncMock(),
            "redeploy_all_services": AsyncMock(return_value=(0, 0, [])),
        }.items():
            monkeypatch.setattr(handlers, name, fake)

    async def succeed(self, *, proof=PROOF, manager=None, episode="episode-1"):
        return await handlers.handle_provisioning_success(
            "vps-9",
            "203.0.113.9",
            1,
            episode,
            False,
            ssh_manager=manager or _manager(),
            qa_target_proof=proof,
            expected_identity=target_identity(_row(), None),
        )

    def failure(self) -> tuple:
        (recorded,) = self.row["incidents"]
        return recorded


@pytest.fixture
def api(monkeypatch):
    return Provisioner(monkeypatch)


class TestTheSuccessOrder:
    async def test_one_call_carries_key_phase_receipt_and_ready_evidence(self, api):
        result = await api.succeed()

        assert result["provisioning_result"]["status"] == "success"
        assert api.calls == ["finalize"]
        (finalization,) = api.finalizations
        receipt = finalization.qa_target_receipt
        assert receipt.profile_version == QA_TARGET_PROFILE_VERSION
        assert receipt.proved_at == PROOF.proved_at
        # Bound to the key this handler just persisted, not to anything older.
        assert receipt.identity.ssh_key_fingerprint == GENERATED_FINGERPRINT
        assert receipt.identity.public_ip == "203.0.113.9"
        assert api.row["status"] == "ready"
        assert finalization.generated_private_key == GENERATED_KEY
        assert finalization.complete_labels == {
            "provisioning_phase": "complete",
            "qa_ssh_user": "qa-observer",
        }

    async def test_an_exact_duplicate_is_idempotent_and_publishes_nothing_new(self, api):
        first = await api.succeed()
        api.disposition = ProvisioningFinalizationDisposition.IDEMPOTENT
        second = await api.succeed()

        assert first["provisioning_result"]["status"] == "success"
        assert second["provisioning_result"]["status"] == "success"
        assert api.row["receipt"] is api.finalizations[0].qa_target_receipt
        assert "status:error" not in api.calls

    async def test_an_attempt_a_newer_episode_owns_records_neither_receipt_nor_ready(self, api):
        result = await api.succeed(episode="episode-old")

        assert result["provisioning_result"]["status"] == "superseded"
        assert api.row["receipt"] is None
        assert api.row["status"] == "provisioning"


class TestEveryOtherOutcomeFailsClosed:
    async def test_a_play_that_proved_no_current_profile_is_not_ready(self, api):
        result = await api.succeed(proof=None)

        assert result["provisioning_result"]["status"] == "failed"
        assert "finalize" not in api.calls
        assert api.row["status"] == "error"
        assert api.failure() == (
            IncidentType.PROVISIONING_FAILED,
            {"step": "qa_target_receipt", "reason": "qa_target_profile_not_proved"},
        )

    async def test_an_identity_that_changed_before_the_receipt_landed_is_not_ready(self, api):
        api.disposition = ProvisioningFinalizationDisposition.CONFLICT

        result = await api.succeed()

        assert result["provisioning_result"]["status"] == "superseded"
        assert api.row["receipt"] is None
        assert api.row["status"] == "provisioning"
        assert not api.row["incidents"]

    async def test_an_unknown_finalizer_outcome_does_not_write_a_failure(self, api):
        from src.provisioner.handlers import FinalizationOutcomeUnknown

        api.finalize_error = RuntimeError("API unavailable")

        with pytest.raises(FinalizationOutcomeUnknown):
            await api.succeed()

        assert api.row["receipt"] is None
        assert api.row["status"] == "provisioning"
        assert not api.row["incidents"]

    async def test_a_generated_key_that_does_not_parse_is_never_stored(self, api):
        api.disposition = ProvisioningFinalizationDisposition.CONTAINED
        result = await api.succeed(manager=_manager("PRIVATE-KEY"))

        assert result["provisioning_result"]["reason"] == "finalization_contained"
        assert api.row["key"] is None
        assert api.failure()[1] == {
            "step": "qa_target_receipt",
            "reason": "finalization_contained",
        }

    async def test_a_retry_after_a_contained_finalization_can_still_succeed(self, api):
        api.disposition = ProvisioningFinalizationDisposition.CONTAINED
        await api.succeed()
        api.disposition = ProvisioningFinalizationDisposition.FINALIZED
        api.row["incidents"].clear()

        result = await api.succeed()

        assert result["provisioning_result"]["status"] == "success"
        assert api.row["status"] == "ready"
        assert len(api.finalizations) == 2


class TestOnlyTheProvedIdentityIsRecorded:
    async def test_a_proof_made_through_another_key_is_never_recorded(self, api):
        """The BitLaunch false positive: proved with one key, about to store another."""
        api.disposition = ProvisioningFinalizationDisposition.CONTAINED
        other = QATargetProof(
            profile_version=QA_TARGET_PROFILE_VERSION,
            proved_at=PROOF.proved_at,
            ssh_user="root",
            ssh_key_fingerprint="SHA256:the-provider-creation-key",
        )

        result = await api.succeed(proof=other)

        assert result["provisioning_result"]["status"] == "failed"
        assert api.calls == ["finalize", "status:error"]
        assert api.row["receipt"] is None
        assert api.row["status"] == "error"
        assert api.failure()[1] == {
            "step": "qa_target_receipt",
            "reason": "finalization_contained",
        }

    async def test_a_proof_for_an_account_the_row_does_not_administer_is_not_recorded(self, api):
        api.disposition = ProvisioningFinalizationDisposition.CONFLICT
        other = QATargetProof(
            profile_version=QA_TARGET_PROFILE_VERSION,
            proved_at=PROOF.proved_at,
            ssh_user="deploy",
            ssh_key_fingerprint=GENERATED_FINGERPRINT,
        )

        result = await api.succeed(proof=other)

        assert api.calls == ["finalize"]
        assert api.row["receipt"] is None
        assert result["provisioning_result"]["status"] == "superseded"
        assert not api.row["incidents"]


class TestTheProvisioningPathsCarryTheProof:
    def test_only_a_proof_of_the_current_profile_is_carried(self):
        identity = {"ssh_user": "root", "ssh_key_fingerprint": GENERATED_FINGERPRINT}

        proof = current_profile_proof(PROOF_OUTPUT, **identity)

        assert proof.profile_version == QA_TARGET_PROFILE_VERSION
        assert (proof.ssh_user, proof.ssh_key_fingerprint) == ("root", GENERATED_FINGERPRINT)
        assert current_profile_proof("PLAY RECAP ok=40", **identity) is None
        assert (
            current_profile_proof(
                PROOF_OUTPUT.replace(QA_TARGET_PROFILE_VERSION, "0" * 16), **identity
            )
            is None
        )

    async def test_fresh_provisioning_hands_the_software_proof_to_the_success_handler(
        self, monkeypatch
    ):
        ansible = MagicMock()
        ansible.run_playbook.return_value = (True, PROOF_OUTPUT)
        node = ProvisionerNode(ssh_manager=_manager(), ansible_runner=ansible)
        success = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
        monkeypatch.setattr("src.provisioner.node.handle_provisioning_success", success)
        monkeypatch.setattr("src.provisioner.node.update_server_labels", AsyncMock())

        await node._run_existing_access_path(
            "vps-9",
            "203.0.113.9",
            "deploy",
            1,
            "episode-1",
            False,
            {"errors": []},
            expected_identity=target_identity(_row(ssh_user="deploy"), None),
        )

        proof = success.await_args.kwargs["qa_target_proof"]
        assert proof.profile_version == QA_TARGET_PROFILE_VERSION
        # Proved through the generated key, as the row's administrative account.
        assert proof.ssh_key_fingerprint == GENERATED_FINGERPRINT
        assert proof.ssh_user == "deploy"

    async def test_reinstall_hands_its_proof_to_the_success_handler(self, monkeypatch):
        node = ProvisionerNode(ssh_manager=_manager(), ansible_runner=MagicMock())
        success = AsyncMock(return_value={"provisioning_result": {"status": "success"}})
        monkeypatch.setattr("src.provisioner.node.handle_provisioning_success", success)
        monkeypatch.setattr(
            "src.provisioner.node.reinstall_and_provision",
            AsyncMock(return_value=ReinstallOutcome(True, "ok", PROOF)),
        )

        await node._run_reinstall_path(
            time4vps_client=SimpleNamespace(),
            server_handle="vps-9",
            provider="time4vps",
            server_id=9,
            server_ip="203.0.113.9",
            deploy_user="deploy",
            os_template="ubuntu",
            provisioning_attempts=1,
            provisioning_episode_id="episode-1",
            expected_identity=target_identity(_row(ssh_user="deploy"), None),
            is_recovery=False,
            state={"errors": []},
        )

        assert success.await_args.kwargs["qa_target_proof"] is PROOF
