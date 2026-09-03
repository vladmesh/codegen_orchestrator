"""DevOps subgraph state definition."""

from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from shared.contracts.queues.deploy import DeployOutcome


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

    # Messages passed between deploy nodes
    messages: Annotated[list, add_messages]

    # Input (from parent graph)
    project_id: str | None
    run_id: str | None
    project_spec: dict | None
    allocated_resources: dict
    repo_info: dict | None
    provided_secrets: dict  # secrets provided by PO
    env_overrides: dict  # deploy-time literal overrides, see DeployMessage.env_overrides

    # Internal environment resolution state
    environment_contract: dict | None
    resolution_outcome: DeployOutcome | None
    secret_values: dict[str, str]
    non_secret_values: dict[str, str]

    # Deploy target
    head_sha: str | None  # the story's commit, the PR head (from merged PR)
    # The commit that is actually deployed: the built commit on the default
    # branch whose images the target pulls and whose tree it checks out. See
    # DeployMessage.deployed_commit_sha — never the same value as head_sha on a
    # PR-merge deploy, and never a substitute for it.
    deployed_commit_sha: str | None
    # Whether this deploy must be the last writer, see DeployMessage.fence_active_deploys
    fence_active_deploys: bool

    # Output (returned to parent). Each entry is a serialized MissingUserSecret
    # ({"key", "description"}) so the scheduler can name secrets to the user.
    missing_user_secrets: list[dict]
    deployment_result: dict | None
    deployed_url: str | None
    smoke_result: dict | None
    application_id: int | None
    bot_username: str | None
    errors: Annotated[list[str], _merge_errors]
