from pydantic import ValidationError
import pytest

from shared.contracts.queues.provisioner import ProvisionerMessage


def test_provisioner_profile_is_an_optional_typed_request_flag():
    assert ProvisionerMessage(server_handle="target").profile is None
    assert ProvisionerMessage(server_handle="target", profile="stand_e2e").profile == "stand_e2e"

    with pytest.raises(ValidationError):
        ProvisionerMessage(server_handle="target", profile="from-a-label")
