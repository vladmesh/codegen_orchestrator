"""Docker capability - enables Docker-in-Docker via Sysbox."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.DOCKER)
class DockerCapability(CapabilityFactory):
    """Docker-in-Docker capability using Sysbox runtime.

    This capability enables running Docker inside the agent container
    using the Sysbox runtime for secure, unprivileged Docker-in-Docker.

    Note: This requires the host to have Sysbox runtime installed.
    The container will be started with --runtime=sysbox-runc flag.
    """

    def get_apt_packages(self) -> list[str]:
        """Docker will be available via Sysbox, no packages needed."""
        return []

    def get_install_commands(self) -> list[str]:
        """Docker is pre-installed in Sysbox containers."""
        return []

    def get_env_vars(self) -> dict[str, str]:
        """No additional env vars needed."""
        return {}
