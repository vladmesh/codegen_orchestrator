"""Dead-layer removal: the legacy langgraph tools and LLM-node layers are gone.

Guards against reintroducing the shadow `src/tools/{projects,servers,github,specs}.py`
package or the second agent-config cache, and pins the live allocator's new home.
Also guards the `LLMNode` cluster (`ToolExecutor`, `LLMFactory`, `get_agent_config`),
the unused `redis_publisher`, the two unmounted API routers and the two alias blocks.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[4]
API_ROUTERS = REPO_ROOT / "services/api/src/routers"


@pytest.mark.parametrize(
    "module",
    [
        "src.tools",
        "src.schemas.tools",
        "src.config.agent_config_cache",
        "src.subgraphs.devops.env_analyzer",
        "src.subgraphs.devops.env_groups",
        "src.config.agent_config",
        "src.llm",
        "src.llm.factory",
        "src.nodes.tool_executor",
        "src.redis_publisher",
    ],
)
def test_dead_module_removed(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_llm_node_is_gone_and_functional_node_stays():
    base = importlib.import_module("src.nodes.base")

    assert not hasattr(base, "LLMNode")
    assert hasattr(base, "FunctionalNode")
    assert hasattr(base, "RetryPolicy")


def test_config_package_reexports_nothing_from_agent_config():
    config = importlib.import_module("src.config")

    assert not hasattr(config, "get_agent_config")
    assert not hasattr(config, "invalidate_cache")


def test_unmounted_api_routers_are_gone():
    assert not (API_ROUTERS / "resources.py").exists()

    main = (REPO_ROOT / "services/api/src/main.py").read_text()
    assert "routers.resources" not in main
    assert "routers.available_models" not in main

    package = (API_ROUTERS / "__init__.py").read_text()
    assert '"resources"' not in package
    assert '"available_models"' not in package


def test_available_models_keeps_only_its_live_validator():
    module = (API_ROUTERS / "available_models.py").read_text()

    assert "async def validate_model_identifier" in module
    assert "APIRouter" not in module
    assert "@router" not in module


@pytest.mark.parametrize(
    ("module", "aliases"),
    [
        (
            "rag.py",
            (
                "_verify_ingest_signature",
                "_build_signature",
                "_get_encoding",
                "_generate_chunk_embeddings",
                "_search_chunks",
                "_apply_token_budget",
                "_upsert_document",
                "_apply_document_fields",
                "_parse_scope",
                "_resolve_scope_ids",
                "_validate_payload_targets",
                "_hash_text",
                "_chunk_document",
            ),
        ),
        (
            "tasks.py",
            (
                "_commit_or_raise_fk",
                "_generate_id",
                "_to_read",
                "_get_task",
                "_get_last_event_summary",
                "_create_status_event",
                "_validate_transition",
            ),
        ),
    ],
)
def test_router_alias_blocks_are_gone(module, aliases):
    source = (API_ROUTERS / module).read_text()

    for alias in aliases:
        assert alias not in source


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
