"""Tools for managing project specifications."""

from typing import Annotated

from langchain_core.tools import tool
import yaml

from shared.schemas.project_spec import (
    EntryPointSpec,
    InfrastructureSpec,
    ProjectInfo,
    ProjectSpecYAML,
)

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


@tool
async def create_project_spec_yaml(
    repo_full_name: Annotated[str, "Full repository name (org/repo)"],
    project_name: Annotated[str, "Project name in snake_case"],
    description: Annotated[str, "Brief project description"],
    modules: Annotated[list[str], "Service template modules (e.g., backend, tg_bot)"],
    entry_points: Annotated[list[dict], "Entry points with type and optional handlers/port"],
    secrets_required: Annotated[list[str], "Required environment secrets"] = None,
    min_ram_mb: Annotated[int, "Minimum RAM in MB"] = 256,
    min_disk_mb: Annotated[int, "Minimum disk space in MB"] = 512,
) -> str:
    """Create .project-spec.yaml file in the repository.

    This generates a machine-readable project specification for disaster recovery.
    Call this after creating a project to ensure the spec is stored in the repository.

    Args:
        repo_full_name: Repository name (org/repo)
        project_name: Project name
        description: Project description
        modules: List of modules to use
        entry_points: List of entry point dicts with 'type' and optional 'handlers'/'port'
        secrets_required: List of required secrets
        min_ram_mb: Minimum RAM requirement
        min_disk_mb: Minimum disk requirement

    Returns:
        Success or error message
    """
    parts = repo_full_name.split("/")
    if len(parts) != 2:  # noqa: PLR2004
        raise ValueError(f"Invalid repo format: {repo_full_name}. Expected 'org/repo'")

    owner, repo = parts
    client = get_github_client()

    try:
        # Build spec model
        project_info = ProjectInfo(name=project_name, description=description)
        entry_point_specs = [EntryPointSpec(**ep) for ep in entry_points]
        infrastructure = InfrastructureSpec(
            min_ram_mb=min_ram_mb,
            min_disk_mb=min_disk_mb,
            ports=[ep.get("port") for ep in entry_points if ep.get("port")],
        )

        spec = ProjectSpecYAML(
            project=project_info,
            modules=modules,
            entry_points=entry_point_specs,
            secrets_required=secrets_required or [],
            infrastructure=infrastructure,
        )

        # Convert to YAML
        yaml_content = yaml.dump(
            spec.to_yaml_dict(),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

        # Commit to repository
        await client.create_or_update_file(
            owner=owner,
            repo=repo,
            path=".project-spec.yaml",
            content=yaml_content,
            message="Add project specification",
            branch="main",
        )

        return ".project-spec.yaml created successfully."
    except Exception as e:
        return f"Failed to create .project-spec.yaml: {str(e)}"
