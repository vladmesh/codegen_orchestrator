from typing import Literal

from shared.contracts.base import BaseMessage, BaseResult

ProvisioningProfile = Literal["stand_e2e"]


class ProvisionerMessage(BaseMessage):
    """Provision server."""

    server_handle: str  # Cloud provider ID (Droplet ID) or unique identifier
    is_recovery: bool = False
    # This is an immutable request-scoped execution choice. It must not be
    # inferred from server labels, which can change after the job is queued.
    profile: ProvisioningProfile | None = None


class ProvisionerResult(BaseResult):
    """
    Provisioning result.
    Stream: provisioner:results
    Consumers: scheduler (update DB), telegram-bot (notify admin)
    """

    server_handle: str
    server_ip: str | None = None
    services_redeployed: int = 0
    errors: list[str] | None = None
