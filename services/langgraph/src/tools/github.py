"""GitHub Tools for agents - repository management."""

from typing import Annotated

from langchain_core.tools import tool

from shared.clients.github import GitHubAppClient

from ..schemas.tools import GitHubRepoResult

# Singleton client instance
_github_client: GitHubAppClient | None = None


def get_github_client() -> GitHubAppClient:
    """Get or create GitHub client instance."""
    global _github_client
    if _github_client is None:
        _github_client = GitHubAppClient()
    return _github_client


@tool
async def create_github_repo(
    name: Annotated[str, "Repository name in snake_case or kebab-case"],
    description: Annotated[str, "Brief repository description"],
) -> GitHubRepoResult:
    """Create a new GitHub repository in the organization.

    Automatically detects the organization from GitHub App installation.
    Creates a private repository with auto-initialized README.
    Returns repository details including clone URL.
    """
    client = get_github_client()

    # Auto-detect organization from GitHub App installation
    installation = await client.get_first_org_installation()
    org = installation["org"]

    repo_data = await client.create_repo(
        org=org,
        name=name,
        description=description,
        private=True,
    )

    return GitHubRepoResult(
        name=repo_data.name,
        full_name=repo_data.full_name,
        html_url=repo_data.html_url,
        clone_url=repo_data.clone_url,
        default_branch=repo_data.default_branch,
    )


@tool
async def get_github_token(
    repo_full_name: Annotated[str, "Full repository name (org/repo)"],
) -> str:
    """Get a GitHub installation token for a repository.

    This token can be used for git operations (clone, push).
    Token is short-lived and scoped to the specific repository.
    """
    parts = repo_full_name.split("/")
    expected_parts = 2
    if len(parts) != expected_parts:
        raise ValueError(f"Invalid repo format: {repo_full_name}. Expected 'org/repo'")

    owner, repo = parts
    client = get_github_client()
    return await client.get_token(owner, repo)
