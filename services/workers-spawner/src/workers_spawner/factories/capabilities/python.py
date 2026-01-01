"""Python capability factory."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.PYTHON)
class PythonCapability(CapabilityFactory):
    """Provides Python 3 runtime capability."""

    def get_apt_packages(self) -> list[str]:
        """Python 3 and pip."""
        return ["python3", "python3-pip", "python3-venv"]

    def get_install_commands(self) -> list[str]:
        """Upgrade pip to latest version.

        Note: Uses --break-system-packages for Debian 12+ compatibility (PEP 668).
        This is safe in a container environment.
        """
        return [
            "python3 -m pip install --upgrade pip --break-system-packages",
        ]

    def get_env_vars(self) -> dict[str, str]:
        """Disable pip warnings about running as root."""
        return {
            "PIP_ROOT_USER_ACTION": "ignore",
        }
