"""Repository setup: create the GitHub repo and make its registry secrets current."""

from __future__ import annotations

import os

import httpx
import structlog

from shared.contracts.dto.project import ProjectDTO
from shared.redis import RedisStreamClient

from ..clients.api import api_client
from ..runtime_identity import project_runtime_slug

logger = structlog.get_logger(__name__)


async def _create_repo_and_set_secrets(project: ProjectDTO, redis: RedisStreamClient) -> None:
    """Create a draft project's GitHub repo and make its registry secrets current.

    Copier and make setup follow later in worker-manager's scaffold phase, whose push
    starts the product's CI; a failure here raises, so no repository is recorded.
    """
    from shared.clients.github import GitHubAppClient, registry_writer_lease_seconds

    project_id = str(project.id)
    project_name = project.title

    org_name = os.getenv("GITHUB_ORG")
    if not org_name:
        raise RuntimeError("GITHUB_ORG environment variable is not set")

    repo_name = project_runtime_slug(project)
    repo_full_name = f"{org_name}/{repo_name}"

    github_client = GitHubAppClient()

    # Step 1: Create repository. A 422 is only classified as stale existing-repo
    # state after a read proves that repository exists. Typed HTTP/transport
    # failures keep their original type; an unexpected client implementation
    # failure is wrapped without reclassifying it from its message text.
    logger.info("creating_repo", org=org_name, repo=repo_name)
    try:
        await github_client.create_repo(
            org=org_name,
            name=repo_name,
            description=f"Project: {project_name}",
            private=True,
        )
        logger.info("repo_created", repo=repo_full_name)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
            raise
        try:
            await github_client.get_repo(org_name, repo_name)
        except httpx.HTTPStatusError as lookup_exc:
            if lookup_exc.response.status_code == httpx.codes.NOT_FOUND:
                raise exc from lookup_exc
            raise
        raise RuntimeError(
            f"Repository {repo_full_name} already exists. "
            "This likely means a previous run was not cleaned up. "
            "Delete the repo and retry, or create a new project."
        ) from exc
    except httpx.HTTPError:
        raise
    except Exception as exc:
        raise RuntimeError(f"GitHub repository creation failed: {exc}") from exc

    # Step 2: The scaffold push starts the product's CI, whose build-and-push logs in
    # with the registry secrets, so they are current before it or the setup fails.
    registry = await github_client.ensure_registry_secrets(
        org_name,
        repo_name,
        redis=redis.redis,
        lease_seconds=registry_writer_lease_seconds(),
    )
    logger.info("registry_secrets_current", repo=repo_full_name, written=registry.written)

    # Step 3: Create Repository entity (project stays DRAFT until scaffold completes)
    repo_url = f"https://github.com/{repo_full_name}"

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
