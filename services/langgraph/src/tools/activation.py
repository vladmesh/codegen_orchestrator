"""Project activation tools for PO Supervisor flow."""

from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client


def _parse_env_example(content: str) -> list[str]:
    """Parse .env.example and extract variable names."""
    variables = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            var_name = line.split("=")[0].strip()
            if var_name:
                variables.append(var_name)
    return variables


@tool
async def activate_project(
    project_id: Annotated[str, "Project ID to activate for deployment"],
) -> dict[str, Any]:
    """Activate a discovered project for deployment.

    Changes project status to 'setup_required' and inspects the repository
    to determine what secrets/configuration are needed.

    Use when user says "launch X" or "activate X" for a discovered project.

    Returns:
        - status: new project status
        - required_secrets: list of env vars from .env.example
        - missing_secrets: secrets not yet configured
        - has_docker_compose: whether project has docker-compose.yml
        - repo_info: repository info (full_name, html_url, clone_url)
    """
    # Import here to avoid circular dependency
    from .github import get_github_client

    # Get current project
    resp = await api_client.get_raw(f"/projects/{project_id}")
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found"}
    resp.raise_for_status()
    project = resp.json()

    # Update status to setup_required
    await api_client.patch(
        f"/projects/{project_id}",
        json={"status": "setup_required"},
    )

    # Inspect repository
    inspection = await inspect_repository.ainvoke({"project_id": project_id})

    # Build repo_info for DevOps
    project_name = project.get("name", project_id)
    repo_info = None
    try:
        github = get_github_client()
        installation = await github.get_first_org_installation()
        org = installation["org"]
        repo_info = {
            "full_name": f"{org}/{project_name}",
            "html_url": f"https://github.com/{org}/{project_name}",
            "clone_url": f"https://github.com/{org}/{project_name}.git",
        }
    except Exception:
        # Non-fatal: DevOps will fail later with a clear message
        pass

    return {
        "project_id": project_id,
        "project_name": project_name,
        "status": "setup_required",
        "required_secrets": inspection.get("required_secrets", []),
        "missing_secrets": inspection.get("missing_secrets", []),
        "has_docker_compose": inspection.get("has_docker_compose", False),
        "repo_info": repo_info,
    }


@tool
async def inspect_repository(
    project_id: Annotated[str, "Project ID to inspect"],
) -> dict[str, Any]:
    """Inspect a project's repository to determine deployment requirements.

    Fetches .env.example to determine required secrets,
    checks for docker-compose.yml, and compares against configured secrets.

    Returns:
        - required_secrets: list of env var names from .env.example
        - missing_secrets: secrets that are not yet configured
        - has_docker_compose: whether docker-compose.yml exists
        - files: list of files in repo root
    """
    # Import here to avoid circular dependency
    from .github import get_github_client

    # Get project to find repo info
    resp = await api_client.get_raw(f"/projects/{project_id}")
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found"}
    resp.raise_for_status()
    project = resp.json()

    # Determine repo owner/name from project name (convention: org/project_name)
    project_name = project.get("name", project_id)
    config = project.get("config", {}) or {}
    existing_secrets = config.get("secrets", {}) or {}

    # Try to get GitHub client and inspect repo
    try:
        github = get_github_client()
        installation = await github.get_first_org_installation()
        org = installation["org"]

        # List files in root
        files = await github.list_repo_files(org, project_name)

        # Check for .env.example
        env_content = await github.get_file_contents(org, project_name, ".env.example")
        required_secrets = _parse_env_example(env_content) if env_content else []

        # Determine missing secrets
        missing_secrets = [s for s in required_secrets if s not in existing_secrets]

        # Check for docker-compose.yml
        has_docker_compose = "docker-compose.yml" in files or "docker-compose.yaml" in files

        return {
            "project_id": project_id,
            "required_secrets": required_secrets,
            "missing_secrets": missing_secrets,
            "has_docker_compose": has_docker_compose,
            "files": files[:20],  # Limit to first 20 files
        }
    except Exception as e:
        return {
            "project_id": project_id,
            "error": f"Failed to inspect repository: {e}",
            "required_secrets": [],
            "missing_secrets": [],
            "has_docker_compose": False,
            "files": [],
        }


@tool
async def save_project_secret(
    project_id: Annotated[str, "Project ID"],
    key: Annotated[str, "Secret key name (e.g., TELEGRAM_TOKEN)"],
    value: Annotated[str, "Secret value"],
) -> dict[str, Any]:
    """Save a secret for a project's deployment.

    Stores the secret in project.config.secrets.
    Call this after user provides a required secret.

    Returns confirmation and updated missing secrets list.
    """
    # Get current project
    resp = await api_client.get_raw(f"/projects/{project_id}")
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found"}
    resp.raise_for_status()
    project = resp.json()

    # Update secrets in config
    config = project.get("config", {}) or {}
    secrets = config.get("secrets", {}) or {}
    secrets[key] = value
    config["secrets"] = secrets

    # Save updated config
    await api_client.patch(
        f"/projects/{project_id}",
        json={"config": config},
    )

    # Re-inspect to get updated missing secrets
    inspection = await inspect_repository.ainvoke({"project_id": project_id})

    return {
        "saved": True,
        "key": key,
        "project_id": project_id,
        "missing_secrets": inspection.get("missing_secrets", []),
    }


@tool
async def check_ready_to_deploy(
    project_id: Annotated[str, "Project ID to check"],
) -> dict[str, Any]:
    """Check if a project has all requirements for deployment.

    Verifies all secrets are configured and repo is ready.
    If ready, returns ready=True and the deployment can proceed.

    Returns:
        - ready: bool - whether project can be deployed
        - missing: list of missing items
        - project_name: for reference
    """
    # Get project
    resp = await api_client.get_raw(f"/projects/{project_id}")
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found", "ready": False}
    resp.raise_for_status()
    project = resp.json()

    # Inspect repo for requirements
    inspection = await inspect_repository.ainvoke({"project_id": project_id})

    missing = inspection.get("missing_secrets", [])
    ready = len(missing) == 0

    return {
        "ready": ready,
        "project_id": project_id,
        "project_name": project.get("name"),
        "missing": missing,
        "has_docker_compose": inspection.get("has_docker_compose", False),
    }
