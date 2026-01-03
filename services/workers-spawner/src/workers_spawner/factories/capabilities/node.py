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
        """Node.js is pre-installed in universal-worker base image.

        Create symlink to npm global bin in .local/bin (which is already in PATH).
        """
        return [
            "mkdir -p /home/worker/.npm-global/bin",
            "mkdir -p /home/worker/.local/bin",
        ]

    def get_env_vars(self) -> dict[str, str]:
        """Set npm global directory and add to PATH."""
        path_dirs = [
            "/home/worker/.npm-global/bin",
            "/home/worker/.local/bin",
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
        return {
            "NPM_CONFIG_PREFIX": "/home/worker/.npm-global",
            "PATH": ":".join(path_dirs),
        }
