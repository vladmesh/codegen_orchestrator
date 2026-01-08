"""GitHub capability for git operations with authentication."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.GITHUB)
class GitHubCapability(CapabilityFactory):
    """Adds GitHub authentication for git push/pull operations.

    Requires GITHUB_TOKEN in env_vars for authentication.
    Works with GitHub App installation tokens or PATs.
    """

    def get_apt_packages(self) -> list[str]:
        """Git is already provided by GitCapability."""
        return []

    def get_install_commands(self) -> list[str]:
        """No additional install commands needed."""
        return []


def get_github_setup_commands(env_vars: dict[str, str]) -> list[str]:
    """Get git credential setup commands.

    Called by ContainerService after container creation.
    Sets up git credential store with GitHub token from env vars.

    Args:
        env_vars: Environment variables dict (should contain GITHUB_TOKEN)

    Returns:
        List of shell commands to setup git credentials
    """
    token = env_vars.get("GITHUB_TOKEN")
    if not token:
        return []

    return [
        "git config --global credential.helper store",
        # Token is in env, so use variable reference
        'echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > ~/.git-credentials',
        'git config --global user.email "bot@vladmesh.dev"',
        'git config --global user.name "Codegen Bot"',
    ]
