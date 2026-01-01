"""Node.js capability factory."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.NODE)
class NodeCapability(CapabilityFactory):
    """Provides Node.js runtime capability."""

    def get_apt_packages(self) -> list[str]:
        """Base packages needed for nodejs setup."""
        return ["curl", "ca-certificates"]

    def get_install_commands(self) -> list[str]:
        """Install Node.js via nodesource (LTS version)."""
        return [
            "curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -",
            "apt-get install -y nodejs",
        ]

    def get_env_vars(self) -> dict[str, str]:
        """Set npm global directory."""
        return {
            "NPM_CONFIG_PREFIX": "/home/node/.npm-global",
        }
