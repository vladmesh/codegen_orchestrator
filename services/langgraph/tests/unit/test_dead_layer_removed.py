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
        "src.subgraphs.devops.env_groups",
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
    deploy_files = [
        *devops_dir.glob("*.py"),
        *(devops_dir.parents[1] / "consumers").glob("deploy*.py"),
        devops_dir.parents[1] / "nodes/resource_allocator.py",
    ]
    deploy_path = "\n".join(file.read_text() for file in deploy_files)

    forbidden_dependencies = (
        "LLMFactory",
        "ChatOpenAI",
        "get_agent_config",
    )

    assert not any(dependency in deploy_path for dependency in forbidden_dependencies)


def test_legacy_environment_classification_state_is_removed():
    annotations = importlib.import_module("src.subgraphs.devops.state").DevOpsState.__annotations__

    assert "env_analysis" not in annotations
    assert "env_variables" not in annotations
    assert "resolved_secrets" not in annotations


def test_devops_classification_agent_config_is_removed():
    config = Path(__file__).parents[4] / "scripts/agent_configs.yaml"

    assert config.read_text().strip() == "[]"
