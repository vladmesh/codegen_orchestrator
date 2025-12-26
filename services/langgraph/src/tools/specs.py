"""Tools for managing project specifications."""

from typing import Annotated

from langchain_core.tools import tool

from .github import get_github_client


@tool
async def get_project_spec(
    repo_full_name: Annotated[str, "Full repository name (org/repo)"],
) -> str:
    """Read the project specification (SPEC.md) from the repository.

    Returns the content of SPEC.md.
    """
    parts = repo_full_name.split("/")
    if len(parts) != 2:  # noqa: PLR2004
        raise ValueError(f"Invalid repo format: {repo_full_name}. Expected 'org/repo'")

    owner, repo = parts
    client = get_github_client()

    content = await client.get_file_contents(owner, repo, "SPEC.md")
    if content is None:
        return "SPEC.md not found in the repository."
    return content


@tool
async def update_project_spec(
    repo_full_name: Annotated[str, "Full repository name (org/repo)"],
    content: Annotated[str, "The full updated content of SPEC.md"],
    update_description: Annotated[str, "Short description of what changed (for commit message)"],
) -> str:
    """Update the project specification (SPEC.md) in the repository.

    This commits the changes to the repository.
    ALWAYS use this tool when the user requests changes to an existing project's requirements.
    """
    parts = repo_full_name.split("/")
    if len(parts) != 2:  # noqa: PLR2004
        raise ValueError(f"Invalid repo format: {repo_full_name}. Expected 'org/repo'")

    owner, repo = parts
    client = get_github_client()

    try:
        await client.create_or_update_file(
            owner=owner,
            repo=repo,
            path="SPEC.md",
            content=content,
            message=f"Update SPEC.md: {update_description}",
            branch="main",
        )
        return "SPEC.md updated successfully."
    except Exception as e:
        return f"Failed to update SPEC.md: {str(e)}"
