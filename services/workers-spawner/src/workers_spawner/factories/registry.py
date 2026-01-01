"""Factory registry for agents and capabilities."""

from workers_spawner.factories.base import AgentFactory, CapabilityFactory
from workers_spawner.models import AgentType, CapabilityType

# Global registries
_AGENT_REGISTRY: dict[AgentType, type[AgentFactory]] = {}
_CAPABILITY_REGISTRY: dict[CapabilityType, type[CapabilityFactory]] = {}


def register_agent(agent_type: AgentType):
    """Decorator to register an agent factory.

    Usage:
        @register_agent(AgentType.CLAUDE_CODE)
        class ClaudeCodeAgent(AgentFactory):
            ...
    """

    def decorator(cls: type[AgentFactory]) -> type[AgentFactory]:
        _AGENT_REGISTRY[agent_type] = cls
        return cls

    return decorator


def register_capability(capability_type: CapabilityType):
    """Decorator to register a capability factory.

    Usage:
        @register_capability(CapabilityType.GIT)
        class GitCapability(CapabilityFactory):
            ...
    """

    def decorator(cls: type[CapabilityFactory]) -> type[CapabilityFactory]:
        _CAPABILITY_REGISTRY[capability_type] = cls
        return cls

    return decorator


def get_agent_factory(agent_type: AgentType) -> AgentFactory:
    """Get an agent factory instance by type.

    Args:
        agent_type: The type of agent to get factory for.

    Returns:
        Instance of the appropriate AgentFactory.

    Raises:
        ValueError: If agent type is not registered.
    """
    if agent_type not in _AGENT_REGISTRY:
        available = list(_AGENT_REGISTRY.keys())
        raise ValueError(f"Unknown agent type: {agent_type}. Available: {available}")
    return _AGENT_REGISTRY[agent_type]()


def get_capability_factory(capability_type: CapabilityType) -> CapabilityFactory:
    """Get a capability factory instance by type.

    Args:
        capability_type: The type of capability to get factory for.

    Returns:
        Instance of the appropriate CapabilityFactory.

    Raises:
        ValueError: If capability type is not registered.
    """
    if capability_type not in _CAPABILITY_REGISTRY:
        available = list(_CAPABILITY_REGISTRY.keys())
        raise ValueError(f"Unknown capability type: {capability_type}. Available: {available}")
    return _CAPABILITY_REGISTRY[capability_type]()
