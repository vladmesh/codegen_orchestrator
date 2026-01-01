"""Curl capability factory."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import CapabilityType


@register_capability(CapabilityType.CURL)
class CurlCapability(CapabilityFactory):
    """Provides HTTP request capability via curl."""

    def get_apt_packages(self) -> list[str]:
        """Curl and CA certificates."""
        return ["curl", "ca-certificates"]
