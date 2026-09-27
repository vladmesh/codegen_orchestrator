"""GitHub sync worker - syncs projects and their status from GitHub."""

import asyncio
import os
import time

from pydantic import ValidationError
import structlog
import yaml

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.project import ProjectDTO, ProjectStatus, ProjectUpdate
from shared.contracts.dto.repository import RepositoryStatus
from shared.notifications import notify_admins_best_effort
from shared.schemas.github import GitHubRepository
from shared.schemas.project_spec import ProjectSpecYAML
from src.clients.api import api_client

from .. import startup

logger = structlog.get_logger()


def _sync_interval() -> int:
    return startup.get_config().get_int("scheduler.github_sync_interval")


def _missing_threshold() -> int:
    return startup.get_config().get_int("scheduler.github_sync_missing_threshold")


async def _sync_project_spec(
    github_client: GitHubAppClient,
    project: ProjectDTO,
    r: GitHubRepository,
) -> None:
    """Sync the project spec from the repository's .project-spec.yaml."""
    owner, repo = r.full_name.split("/")

    # Sync project spec from .project-spec.yaml
    try:
        spec_content = await github_client.get_file_contents(owner, repo, ".project-spec.yaml")
        if spec_content:
            spec_dict = yaml.safe_load(spec_content)
            spec_model = ProjectSpecYAML(**spec_dict)

            # Update project spec via API
            await api_client.update_project(
                project.id, ProjectUpdate(project_spec=spec_model.to_yaml_dict())
            )

            logger.info(
                "project_spec_synced",
                project_name=project.title,
                spec_version=spec_dict.get("version", "unknown"),
            )
    except ValidationError as e:
        logger.error(
            "project_spec_validation_failed",
            project_name=project.title,
            error=str(e),
        )
        await notify_admins_best_effort(
            f"⚠️ Invalid Specification for *{project.title}*\n"
            f"The `.project-spec.yaml` file is invalid:\n"
            f"```\n{str(e)[:1000]}\n```",
            level="warning",
            component="github_sync",
            project_id=str(project.id),
        )
    except Exception as e:
        logger.debug(
            "project_spec_sync_skipped",
            project_name=project.title,
            error=str(e),
            error_type=type(e).__name__,
        )


async def _sync_single_repo(
    github_client: GitHubAppClient,
    r: GitHubRepository,
    missing_counters: dict[str, int],
) -> None:
    """Sync a single repository to the database."""
    repo_id = r.id
    repo_name = r.name

    # Try to find in DB by Repository.provider_repo_id
    db_repo = await api_client.get_repository_by_provider_id(repo_id)

    if not db_repo:
        # Unknown repo — notify admins, do not create orphan project
        logger.warning(
            "github_repo_without_project",
            repo_name=repo_name,
            provider_repo_id=repo_id,
        )
        await notify_admins_best_effort(
            f"⚠️ Repository *{repo_name}* (GitHub ID: {repo_id}) "
            "found in org but has no matching repository in DB. "
            "Create it manually if needed.",
            level="warning",
            component="github_sync",
            repository_id=repo_id,
        )
        return

    project_id = db_repo.project_id
    project = await api_client.get_project(str(project_id)) if project_id else None
    if not project:
        logger.warning("repo_orphaned", repo_name=repo_name, project_id=project_id)
        return

    await _sync_project_spec(github_client, project, r)

    # Reset missing counter if it was missing
    project_id_str = str(project.id)
    if project_id_str in missing_counters:
        del missing_counters[project_id_str]
        # Recovery: set repository status back to active
        repo_id_str = db_repo.id
        if repo_id_str:
            await api_client.update_repository(
                repo_id_str, {"status": RepositoryStatus.ACTIVE.value}
            )
        logger.info(
            "repository_recovered",
            project_name=project.title,
            provider_repo_id=repo_id,
        )


async def _detect_missing_projects(
    gh_repos_map: dict[int, GitHubRepository],
    missing_counters: dict[str, int],
) -> None:
    """Detect and alert on projects missing from GitHub."""
    db_projects = await api_client.get_projects()

    # For each non-archived project, check if its repositories are present on GitHub
    active_projects = [p for p in db_projects if p.status != ProjectStatus.ARCHIVED]

    for proj in active_projects:
        project_id_str = str(proj.id)
        repos = await api_client.get_repositories(project_id=project_id_str)
        managed_repos = [r for r in repos if r.provider_repo_id is not None]

        if not managed_repos:
            continue  # No repos with provider_repo_id to check

        # Check if any managed repo is missing from GitHub
        all_present = all(r.provider_repo_id in gh_repos_map for r in managed_repos)

        if not all_present:
            count = missing_counters.get(project_id_str, 0) + 1
            missing_counters[project_id_str] = count

            logger.warning(
                "project_missing_from_github",
                project_name=proj.title,
                project_id=project_id_str,
                attempt=count,
                threshold=_missing_threshold(),
            )

            if count >= _missing_threshold():
                # Mark repositories as missing (not the project)
                repos = await api_client.get_repositories(project_id=project_id_str)
                for repo in repos:
                    repo_id = repo.id
                    if repo_id:
                        await api_client.update_repository(
                            repo_id, {"status": RepositoryStatus.MISSING.value}
                        )
                logger.error(
                    "repositories_marked_missing",
                    project_name=proj.title,
                    project_id=project_id_str,
                    attempts=count,
                )
                await notify_admins_best_effort(
                    f"🚨 Project *{proj.title}* is MISSING! "
                    f"Repository not found after {count} consecutive checks.",
                    level="critical",
                    component="github_sync",
                    project_id=project_id_str,
                )


async def sync_projects_worker() -> None:
    """Background worker to sync projects from GitHub."""
    logger.info("github_sync_worker_started")

    # In-memory failure tracking for robust alerting
    missing_counters: dict[str, int] = {}

    while True:
        start_time = time.time()
        repos_synced = 0
        try:
            github_client = GitHubAppClient()

            # 1. Get Organization
            try:
                org_name = os.getenv("GITHUB_ORG")
                if not org_name:
                    install_info = await github_client.get_first_org_installation()
                    org_name = install_info["org"]
            except Exception as e:
                logger.error(
                    "github_app_installation_resolve_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(_sync_interval())
                continue

            logger.info("github_sync_start", org_name=org_name)

            # 2. Fetch all Repositories
            try:
                github_repos = await github_client.list_org_repos(org_name)
            except Exception as e:
                logger.error(
                    "github_repos_fetch_failed",
                    org_name=org_name,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(_sync_interval())
                continue

            logger.info(
                "github_repos_fetched",
                org_name=org_name,
                repo_count=len(github_repos),
            )

            # Map by ID for accurate tracking
            gh_repos_map = {r.id: r for r in github_repos}

            # 3. Sync each repo
            for r in github_repos:
                await _sync_single_repo(github_client, r, missing_counters)
                repos_synced += 1

            # 4. Detect missing projects
            await _detect_missing_projects(gh_repos_map, missing_counters)

            logger.debug("github_sync_db_updated", repo_count=len(github_repos))

        except Exception as e:
            logger.error(
                "github_sync_worker_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
        finally:
            duration = time.time() - start_time
            logger.info(
                "github_sync_complete",
                repos_synced=repos_synced,
                duration_sec=round(duration, 2),
            )

        await asyncio.sleep(_sync_interval())
