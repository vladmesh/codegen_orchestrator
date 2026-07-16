"""DevOps subgraph state definition."""

from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _merge_errors(left: list[str], right: list[str]) -> list[str]:
    """Reducer that merges error lists without duplicates."""
    seen = set(left)
    result = list(left)
    for err in right:
        if err not in seen:
            result.append(err)
            seen.add(err)
    return result


class DevOpsState(TypedDict):
    """State for the DevOps subgraph."""

    # Messages (conversation history for LLM)
    messages: Annotated[list, add_messages]

    # Input (from parent graph)
    project_id: str | None
    run_id: str | None
    project_spec: dict | None
    allocated_resources: dict
    repo_info: dict | None
    provided_secrets: dict  # secrets provided by PO

    # Internal (analysis results)
    env_variables: list[str]  # Raw list of env vars from .env.example
    env_analysis: dict  # {var_name: "infra"|"computed"|"user"}
    environment_contract: dict | None
    resolution_outcome: str | None
    resolved_secrets: dict  # generated/computed secrets

    # Deploy target
    head_sha: str | None  # exact commit SHA to deploy (from merged PR)

    # Output (returned to parent)
    missing_user_secrets: list[str]
    deployment_result: dict | None
    deployed_url: str | None
    smoke_result: dict | None
    application_id: int | None
    bot_username: str | None
    errors: Annotated[list[str], _merge_errors]
