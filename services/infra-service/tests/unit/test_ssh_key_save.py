"""Generated SSH key ownership at the atomic provisioning boundary."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.server import (
    ProvisioningFinalization,
    ProvisioningFinalizationDisposition,
    ProvisioningFinalizationResult,
    QATargetReceipt,
    TargetIdentity,
)
from shared.qa_identity import provisioning_complete_labels
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.provisioner.api_client import finalize_provisioning
from src.provisioner.ssh_manager import SSHManager


class TestSSHManagerGetPrivateKey:
    def test_reads_existing_key(self, tmp_path):
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("fake-private-key-content")

        manager = SSHManager(key_path=str(key_path))

        assert manager.get_private_key() == "fake-private-key-content"

    def test_returns_none_when_no_key(self, tmp_path):
        manager = SSHManager(key_path=str(tmp_path / "nonexistent"))

        assert manager.get_private_key() is None


def _finalization() -> ProvisioningFinalization:
    key = fleet_private_key()
    expected = TargetIdentity(
        ssh_user="root",
        host="srv.example.test",
        public_ip="203.0.113.1",
        ssh_key_fingerprint=None,
    )
    proved = expected.model_copy(
        update={"ssh_key_fingerprint": normalize_admin_private_key(key).fingerprint}
    )
    receipt = QATargetReceipt(
        profile_version=QA_TARGET_PROFILE_VERSION,
        proved_at=datetime.now(UTC),
        identity=proved,
    )
    return ProvisioningFinalization(
        attempt_number=1,
        episode_id="episode-1",
        expected_identity=expected,
        proved_identity=proved,
        generated_key_fingerprint=proved.ssh_key_fingerprint,
        generated_private_key=key,
        complete_labels=provisioning_complete_labels(),
        qa_target_receipt=receipt,
    )


@pytest.mark.asyncio
async def test_finalizer_client_forwards_one_typed_command_and_disposition():
    command = _finalization()
    response = ProvisioningFinalizationResult(
        disposition=ProvisioningFinalizationDisposition.FINALIZED,
        provisioning_attempts=0,
    )
    with patch("src.provisioner.api_client.api_client") as client:
        client.finalize_provisioning = AsyncMock(return_value=response)

        disposition = await finalize_provisioning("srv-1", command)

    assert disposition is ProvisioningFinalizationDisposition.FINALIZED
    client.finalize_provisioning.assert_awaited_once_with("srv-1", command)


@pytest.mark.asyncio
async def test_finalizer_client_propagates_api_error():
    with patch("src.provisioner.api_client.api_client") as client:
        client.finalize_provisioning = AsyncMock(side_effect=RuntimeError("API down"))

        with pytest.raises(RuntimeError, match="API down"):
            await finalize_provisioning("srv-1", _finalization())
