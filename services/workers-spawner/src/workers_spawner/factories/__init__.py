"""Factory abstractions and registry."""

from workers_spawner.factories.base import AgentFactory, CapabilityFactory
from workers_spawner.factories.registry import (
    get_agent_factory,
    get_capability_factory,
    register_agent,
    register_capability,
)

__all__ = [
    "AgentFactory",
    "CapabilityFactory",
    "get_agent_factory",
    "get_capability_factory",
    "register_agent",
    "register_capability",
]
