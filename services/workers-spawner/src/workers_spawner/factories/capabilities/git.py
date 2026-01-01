"""Git capability factory."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.GIT)
class GitCapability(CapabilityFactory):
    """Provides Git version control capability."""

    def get_apt_packages(self) -> list[str]:
        """Git is installed via apt."""
        return ["git"]

    def get_install_commands(self) -> list[str]:
        """Configure git for agent use."""
        return [
            'git config --global user.email "agent@orchestrator.local"',
            'git config --global user.name "Orchestrator Agent"',
            "git config --global init.defaultBranch main",
        ]
