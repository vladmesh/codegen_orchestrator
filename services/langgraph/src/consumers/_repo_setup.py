"""Repository setup — create GitHub repo and configure registry secrets.

Extracted from engineering_worker.py (#18).
"""

from __future__ import annotations

import os
import re

import structlog

from shared.contracts.dto.project import ProjectStatus

from ..clients.api import api_client

logger = structlog.get_logger(__name__)

EXPECTED_REGISTRY_SECRETS_COUNT = 3


async def _create_repo_and_set_secrets(project: dict) -> None:
    """Create GitHub repo and set registry secrets for a draft project.

    Replaces the old scaffolder queue approach: repo creation and secret
    setup happen inline, while copier + make setup are deferred to
    worker-manager's scaffold phase.
    """
    from shared.clients.github import GitHubAppClient

    project_id = project["id"]
    project_name = project.get("name", project_id)

    org_name = os.getenv("GITHUB_ORG")
    if not org_name:
        raise RuntimeError("GITHUB_ORG environment variable is not set")

    # Generate repo name from project name
    repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
    repo_name = re.sub(r"[^a-z0-9-]", "", repo_name)
    repo_name = re.sub(r"-+", "-", repo_name).strip("-")
    if not repo_name:
        repo_name = project_id[:8]
    repo_full_name = f"{org_name}/{repo_name}"

    github_client = GitHubAppClient()

    # Step 1: Create repository (idempotent — handles "already exists")
    logger.info("creating_repo", org=org_name, repo=repo_name)
    try:
        await github_client.create_repo(
            org=org_name,
            name=repo_name,
            description=f"Project: {project_name}",
            private=True,
        )
        logger.info("repo_created", repo=repo_full_name)
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str or "422" in error_str:
            raise RuntimeError(
                f"Repository {repo_full_name} already exists. "
                "This likely means a previous run was not cleaned up. "
                "Delete the repo and retry, or use a different project name."
            ) from e
        else:
            raise

    # Step 2: Set registry secrets so CI can push Docker images
    registry_url = os.getenv("ORCHESTRATOR_HOSTNAME")
    registry_user = os.getenv("REGISTRY_USER")
    registry_password = os.getenv("REGISTRY_PASSWORD")

    if all([registry_url, registry_user, registry_password]):
        token = await github_client.get_org_token(org_name)
        count = await github_client.set_repository_secrets(
            org_name,
            repo_name,
            {
                "REGISTRY_URL": registry_url,
                "REGISTRY_USER": registry_user,
                "REGISTRY_PASSWORD": registry_password,
            },
            token=token,
        )
        if count < EXPECTED_REGISTRY_SECRETS_COUNT:
            logger.warning(
                "registry_secrets_incomplete",
                expected=EXPECTED_REGISTRY_SECRETS_COUNT,
                actual=count,
            )
    else:
        logger.warning(
            "registry_secrets_env_missing",
            has_url=bool(registry_url),
            has_user=bool(registry_user),
            has_password=bool(registry_password),
        )

    # Step 3: Update project status and create Repository entity
    repo_url = f"https://github.com/{repo_full_name}"
    await api_client.patch(
        f"projects/{project_id}",
        json={
            "status": ProjectStatus.SCAFFOLDING.value,
        },
    )

    # Create Repository entity so webhook lookup and developer node work
    await api_client.post(
        "repositories/",
        json={
            "project_id": project_id,
            "name": repo_name,
            "git_url": repo_url,
            "role": "primary",
        },
    )

    logger.info(
        "repo_created_and_secrets_set",
        project_id=project_id,
        repo_full_name=repo_full_name,
    )
