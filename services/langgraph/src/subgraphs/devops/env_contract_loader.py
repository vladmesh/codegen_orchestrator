"""Load the required environment contract for a deploy."""

import structlog
import yaml

from shared.clients.github import GitHubAppClient
from shared.contracts.env_contract import EnvContractMergeError, merge_env_contract_fragments

from .state import DevOpsState

logger = structlog.get_logger()
OWNER_AND_REPOSITORY_PARTS = 2


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Return the GitHub owner and repository name from a repository URL."""
    parts = repo_url.rstrip("/").split("/")
    owner_repo = parts[-2:]
    if len(owner_repo) != OWNER_AND_REPOSITORY_PARTS or not all(owner_repo):
        return None
    return owner_repo[0], owner_repo[1]


async def _fetch_env_contract(owner: str, repo: str, ref: str) -> dict | None:
    """Fetch and validate all committed environment-contract fragments."""
    github = GitHubAppClient()
    paths = await github.list_repo_files_recursive(owner, repo, ref)
    fragment_paths = [path for path in paths if path.endswith("env.contract.yaml")]
    if not fragment_paths:
        return None

    try:
        fragments = []
        for path in fragment_paths:
            content = await github.get_file_contents(owner, repo, path, ref)
            if content is None:
                raise ValueError(f"environment contract fragment disappeared: {path}")
            fragments.append(yaml.safe_load(content))
        return merge_env_contract_fragments(fragments).model_dump(mode="json")
    except (EnvContractMergeError, ValueError, yaml.YAMLError) as error:
        raise ValueError("environment contract is invalid") from error


async def load_environment_contract(state: DevOpsState) -> dict:
    """Load the repository contract or return a terminal contract outcome."""
    project_id = state.get("project_id")
    head_sha = state.get("head_sha")
    if not head_sha:
        logger.error("deploy_head_sha_missing", project_id=project_id)
        return {
            "errors": ["head_sha is required to load the environment contract"],
            "resolution_outcome": "head_sha_missing",
        }

    repo_info = state.get("repo_info") or {}
    repo_url = repo_info.get("html_url", "")
    if not repo_url:
        return {"errors": ["No repository URL found for project"]}

    parsed = _parse_repo_url(repo_url)
    if not parsed:
        return {"errors": [f"Invalid repository URL format: {repo_url}"]}
    owner, repo = parsed

    try:
        contract = await _fetch_env_contract(owner, repo, head_sha)
    except (EnvContractMergeError, ValueError, yaml.YAMLError) as error:
        logger.warning(
            "environment_contract_invalid",
            project_id=project_id,
            error_type=type(error).__name__,
        )
        return {
            "errors": ["environment contract is invalid"],
            "resolution_outcome": "environment_contract_invalid",
        }
    except Exception as error:
        logger.error(
            "environment_contract_fetch_failed",
            project_id=project_id,
            error_type=type(error).__name__,
        )
        return {
            "errors": ["environment contract could not be read"],
            "resolution_outcome": "environment_resolution_failed",
        }

    if contract is None:
        logger.warning("environment_contract_missing", project_id=project_id)
        return {
            "errors": ["environment contract is required"],
            "resolution_outcome": "environment_contract_invalid",
        }

    logger.info(
        "environment_contract_loaded",
        project_id=project_id,
        entry_count=len(contract["entries"]),
    )
    return {"environment_contract": contract}
