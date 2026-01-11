from shared.contracts.base import BaseMessage, BaseResult


class ProvisionerMessage(BaseMessage):
    """Provision server."""

    server_handle: str  # Cloud provider ID (Droplet ID) or unique identifier
    force_reinstall: bool = False
    is_recovery: bool = False


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
