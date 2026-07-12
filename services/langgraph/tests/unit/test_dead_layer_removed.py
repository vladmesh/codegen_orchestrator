"""Phase 3 dead-layer removal: the legacy langgraph tools layer is gone.

Guards against reintroducing the shadow `src/tools/{projects,servers,github,specs}.py`
package or the second agent-config cache, and pins the live allocator's new home.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "src.tools",
        "src.schemas.tools",
        "src.config.agent_config_cache",
    ],
)
def test_dead_module_removed(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_allocator_lives_at_new_location():
    mod = importlib.import_module("src.allocations")
    assert hasattr(mod, "ensure_project_allocations")
    assert hasattr(mod, "AllocationError")
