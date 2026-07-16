"""Phase 3 dead-layer removal: the legacy langgraph tools layer is gone.

Guards against reintroducing the shadow `src/tools/{projects,servers,github,specs}.py`
package or the second agent-config cache, and pins the live allocator's new home.
"""

import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "src.tools",
        "src.schemas.tools",
        "src.config.agent_config_cache",
        "src.subgraphs.devops.env_analyzer",
    ],
)
def test_dead_module_removed(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_allocator_lives_at_new_location():
    mod = importlib.import_module("src.allocations")
    assert hasattr(mod, "ensure_project_allocations")
    assert hasattr(mod, "AllocationError")


def test_deploy_environment_path_has_no_llm_dependency():
    devops_dir = Path(__file__).parents[2] / "src/subgraphs/devops"
    deploy_path = "\n".join(
        (devops_dir / filename).read_text()
        for filename in ("env_contract_loader.py", "graph.py", "secret_resolver.py")
    )

    assert "LLM" not in deploy_path
    assert "llm" not in deploy_path
