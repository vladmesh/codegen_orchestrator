"""Shared utilities for codegen_orchestrator services."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .redis_client import RedisStreamClient


def __getattr__(name: str) -> object:
    """Load optional service integrations only when callers request them."""
    if name == "RedisStreamClient":
        from .redis_client import RedisStreamClient

        return RedisStreamClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Schemas are imported from shared.schemas submodule
# Example: from shared.schemas import RepoInfo, AllocatedResource

__all__ = ["RedisStreamClient"]
