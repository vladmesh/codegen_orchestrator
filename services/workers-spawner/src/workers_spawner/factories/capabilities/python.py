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
        """Upgrade pip to latest version."""
        return [
            "python3 -m pip install --upgrade pip",
        ]

    def get_env_vars(self) -> dict[str, str]:
        """Disable pip warnings about running as root."""
        return {
            "PIP_ROOT_USER_ACTION": "ignore",
        }
