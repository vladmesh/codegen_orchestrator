"""Copier capability factory."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.COPIER)
class CopierCapability(CapabilityFactory):
    """Provides Copier templating capability."""

    def get_apt_packages(self) -> list[str]:
        """No apt packages needed for Copier."""
        return []

    def get_install_commands(self) -> list[str]:
        """Install Copier."""
        return ["pip3 install --break-system-packages copier==9.4.1"]
