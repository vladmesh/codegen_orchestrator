"""Config parser - transforms WorkerConfig into Docker commands."""

# Import to trigger registration
from workers_spawner.factories import (
    agents as _agents,  # noqa: F401
    capabilities as _capabilities,  # noqa: F401
)
from workers_spawner.factories.base import AgentFactory, CapabilityFactory
from workers_spawner.factories.registry import get_agent_factory, get_capability_factory
from workers_spawner.models import WorkerConfig


class ConfigParser:
    """Transforms declarative WorkerConfig into concrete Docker commands.

    Takes a WorkerConfig and uses the appropriate factories to generate:
    - APT packages to install
    - Shell commands for setup
    - Environment variables
    - Agent entrypoint command
    """

    def __init__(self, config: WorkerConfig):
        """Initialize parser with config.

        Args:
            config: The worker configuration to parse.
        """
        self.config = config
        self._agent_factory: AgentFactory = get_agent_factory(config.agent)
        self._capability_factories: list[CapabilityFactory] = [
            get_capability_factory(cap) for cap in config.capabilities
        ]

    def get_apt_packages(self) -> list[str]:
        """Get all APT packages to install.

        Returns:
            Deduplicated list of apt package names.
        """
        packages: set[str] = set()
        for cap_factory in self._capability_factories:
            packages.update(cap_factory.get_apt_packages())
        return sorted(packages)

    def get_install_commands(self) -> list[str]:
        """Get all installation commands.

        Returns:
            Ordered list of shell commands for installation.
        """
        commands: list[str] = []

        # First, capability install commands
        for cap_factory in self._capability_factories:
            commands.extend(cap_factory.get_install_commands())

        # Then, agent install commands
        commands.extend(self._agent_factory.get_install_commands())

        return commands

    def get_env_vars(self) -> dict[str, str]:
        """Get all environment variables.

        Returns:
            Dict of env var name -> value.
        """
        env_vars: dict[str, str] = {}

        # Capability env vars
        for cap_factory in self._capability_factories:
            env_vars.update(cap_factory.get_env_vars())

        # Agent optional env vars (defaults)
        env_vars.update(self._agent_factory.get_optional_env_vars())

        # Config-specified env vars (override)
        env_vars.update(self.config.env_vars)

        # Add ORCHESTRATOR_ALLOWED_TOOLS
        if self.config.allowed_tools:
            env_vars["ORCHESTRATOR_ALLOWED_TOOLS"] = ",".join(self.config.allowed_tools)

        return env_vars

    def get_required_env_vars(self) -> list[str]:
        """Get list of required env var names.

        Returns:
            List of env var names that must be provided.
        """
        return self._agent_factory.get_required_env_vars()

    def get_agent_command(self) -> str:
        """Get the agent entrypoint command.

        Returns:
            Command string to start the agent.
        """
        return self._agent_factory.get_agent_command()

    def get_setup_files(self) -> dict[str, str]:
        """Get files to create during setup.

        Returns:
            Dict of file path -> file content.
        """
        return self._agent_factory.get_setup_files()

    def get_install_script(self) -> str:
        """Generate a complete install script.

        Returns:
            Bash script content for container setup.
        """
        lines: list[str] = [
            "#!/bin/bash",
            "set -e",
            "",
        ]

        # APT packages
        packages = self.get_apt_packages()
        if packages:
            lines.append("# Install APT packages")
            lines.append("apt-get update")
            lines.append(f"apt-get install -y {' '.join(packages)}")
            lines.append("")

        # Install commands
        install_commands = self.get_install_commands()
        if install_commands:
            lines.append("# Run install commands")
            lines.extend(install_commands)
            lines.append("")

        # Env vars export (for interactive use)
        env_vars = self.get_env_vars()
        if env_vars:
            lines.append("# Export environment variables")
            for key, value in env_vars.items():
                lines.append(f'export {key}="{value}"')
            lines.append("")

        return "\n".join(lines)

    def validate(self) -> list[str]:
        """Validate the configuration.

        Note: Required env vars are NOT validated here because they can be
        auto-injected from spawner's environment at container creation time.

        Returns:
            List of validation errors (empty if valid).
        """
        errors: list[str] = []

        # Future validation rules can be added here
        # For now, config structure is validated by Pydantic models

        return errors
