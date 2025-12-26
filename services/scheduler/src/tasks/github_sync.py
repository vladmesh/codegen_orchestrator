"""GitHub sync worker - syncs projects and their status from GitHub."""

import asyncio
import time

from pydantic import ValidationError
from sqlalchemy import select
import structlog
import yaml

from shared.clients.github import GitHubAppClient
from shared.models.project import Project, ProjectStatus
from shared.notifications import notify_admins
from shared.schemas.project_spec import ProjectSpecYAML
from src.db import async_session_maker

logger = structlog.get_logger()

# Config
SYNC_INTERVAL = 300  # 5 minutes
MISSING_THRESHOLD = 3  # Alert after 3 consecutive checks where repo is missing


async def sync_projects_worker():  # noqa: PLR0915
    """Background worker to sync projects from GitHub."""
    logger.info("github_sync_worker_started")

    # In-memory failure tracking for robust alerting
    # {project_id: fail_count}
    missing_counters = {}

    while True:
        start_time = time.time()
        repos_synced = 0
        try:
            async with async_session_maker() as db:
                client = GitHubAppClient()

                # 1. Get Organization
                try:
                    # Try to find the org we are installed on
                    install_info = await client.get_first_org_installation()
                    org_name = install_info["org"]
                except Exception as e:
                    logger.error(
                        "github_app_installation_resolve_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )
                    await asyncio.sleep(SYNC_INTERVAL)
                    continue

                logger.info("github_sync_start", org_name=org_name)

                # 2. Fetch all Repositories
                try:
                    github_repos = await client.list_org_repos(org_name)
                except Exception as e:
                    logger.error(
                        "github_repos_fetch_failed",
                        org_name=org_name,
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )
                    await asyncio.sleep(SYNC_INTERVAL)
                    continue
                logger.info("github_repos_fetched", org_name=org_name, repo_count=len(github_repos))

                # Map by ID for accurate tracking
                # repo_id (int) -> repo_data
                gh_repos_map = {r.id: r for r in github_repos}

                # 3. Sync Logic: GitHub -> DB
                for r in github_repos:
                    repo_id = r.id
                    repo_name = r.name
                    repos_synced += 1

                    # Try to find in DB by github_repo_id
                    query = select(Project).where(Project.github_repo_id == repo_id)
                    result = await db.execute(query)
                    project = result.scalar_one_or_none()

                    if not project:
                        # Try to find by name (legacy projects or first sync)
                        # We use simple matching.
                        query = select(Project).where(Project.name == repo_name)
                        result = await db.execute(query)
                        project = result.scalar_one_or_none()

                        if project:
                            # Link legacy project
                            logger.info(
                                "project_linked_to_github",
                                project_name=project.name,
                                github_repo_id=repo_id,
                            )
                            project.github_repo_id = repo_id
                        else:
                            # Create new project
                            logger.info(
                                "github_project_discovered",
                                project_name=repo_name,
                                github_repo_id=repo_id,
                            )
                            project = Project(
                                id=repo_name,  # Use name as ID for simplicity consistent with usage
                                name=repo_name,
                                github_repo_id=repo_id,
                                status=ProjectStatus.DISCOVERED.value,
                            )
                            db.add(project)

                    # Update metadata
                    # (Can update description, etc if we had those fields)

                    # Sync project spec from .project-spec.yaml
                    try:
                        owner, repo = r.full_name.split("/")
                        spec_content = await client.get_file_contents(
                            owner, repo, ".project-spec.yaml"
                        )
                        if spec_content:
                            # Parse and validate YAML
                            spec_dict = yaml.safe_load(spec_content)
                            # Validate against schema
                            spec_model = ProjectSpecYAML(**spec_dict)
                            # Store in database
                            project.project_spec = spec_model.to_yaml_dict()
                            logger.info(
                                "project_spec_synced",
                                project_name=project.name,
                                spec_version=spec_dict.get("version", "unknown"),
                            )
                    except ValidationError as e:
                        # Validation failed - Notify admins!
                        logger.error(
                            "project_spec_validation_failed",
                            project_name=project.name,
                            error=str(e),
                        )
                        await notify_admins(
                            f"⚠️ Invalid Specification for *{project.name}*\n"
                            f"The `.project-spec.yaml` file is invalid:\n"
                            f"```\n{str(e)[:1000]}\n```",
                            level="warning",
                        )
                    except Exception as e:
                        # Spec sync is non-critical, log and continue
                        logger.debug(
                            "project_spec_sync_skipped",
                            project_name=project.name,
                            error=str(e),
                            error_type=type(e).__name__,
                        )

                    # Reset missing counter if it was missing
                    if project.id in missing_counters:
                        del missing_counters[project.id]
                        if project.status == ProjectStatus.MISSING.value:
                            project.status = (
                                ProjectStatus.ACTIVE.value
                            )  # Or INITIALIZED? Active implies deployed.
                            logger.info(
                                "project_recovered",
                                project_name=project.name,
                                github_repo_id=repo_id,
                            )

                # 4. Sync Logic: DB -> GitHub (Detect Missing)
                # Iterate all projects that SHOULD be on GitHub
                query = select(Project).where(
                    Project.github_repo_id.is_not(None),
                    Project.status.notin_(
                        [ProjectStatus.MISSING.value, ProjectStatus.ARCHIVED.value]
                    ),
                )
                result = await db.execute(query)
                db_projects = result.scalars().all()

                for proj in db_projects:
                    if proj.github_repo_id not in gh_repos_map:
                        # Missing!
                        count = missing_counters.get(proj.id, 0) + 1
                        missing_counters[proj.id] = count

                        logger.warning(
                            "project_missing_from_github",
                            project_name=proj.name,
                            github_repo_id=proj.github_repo_id,
                            attempt=count,
                            threshold=MISSING_THRESHOLD,
                        )

                        if count >= MISSING_THRESHOLD:
                            proj.status = ProjectStatus.MISSING.value
                            logger.error(
                                "project_marked_missing",
                                project_name=proj.name,
                                github_repo_id=proj.github_repo_id,
                                attempts=count,
                            )
                            # Send critical alert to admins
                            await notify_admins(
                                f"🚨 Project *{proj.name}* (GitHub ID: {proj.github_repo_id}) "
                                "is MISSING! "
                                f"Repository not found after {count} consecutive checks.",
                                level="critical",
                            )

                await db.commit()
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

        await asyncio.sleep(SYNC_INTERVAL)
