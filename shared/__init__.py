"""Shared utilities for codegen_orchestrator services."""

try:
    from .redis_client import RedisStreamClient

except Exception:
    RedisStreamClient = None  # type: ignore


# Schemas are imported from shared.schemas submodule
# Example: from shared.schemas import RepoInfo, AllocatedResource

__all__ = ["RedisStreamClient"]
