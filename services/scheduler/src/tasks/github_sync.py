"""GitHub sync worker - syncs projects and their status from GitHub."""

import asyncio
import logging

from sqlalchemy import select

from shared.clients.github import GitHubAppClient

from src.database import async_session_maker
from src.models.project import Project, ProjectStatus

logger = logging.getLogger(__name__)

# Config
SYNC_INTERVAL = 300  # 5 minutes
MISSING_THRESHOLD = 3  # Alert after 3 consecutive checks where repo is missing


async def sync_projects_worker():
    """Background worker to sync projects from GitHub."""
    logger.info("Starting GitHub Sync Worker")

    # In-memory failure tracking for robust alerting
    # {project_id: fail_count}
    missing_counters = {}

    while True:
        try:
            async with async_session_maker() as db:
                client = GitHubAppClient()

                # 1. Get Organization
                try:
                    # Try to find the org we are installed on
                    install_info = await client.get_first_org_installation()
                    org_name = install_info["org"]
                except Exception as e:
                    logger.error(f"Failed to resolve GitHub App installation: {e}")
                    await asyncio.sleep(SYNC_INTERVAL)
                    continue

                logger.info(f"Syncing projects for Organization: {org_name}")

                # 2. Fetch all Repositories
                try:
                    github_repos = await client.list_org_repos(org_name)
                except Exception as e:
                    logger.error(f"Failed to fetch repositories: {e}")
                    await asyncio.sleep(SYNC_INTERVAL)
                    continue

                # Map by ID for accurate tracking
                # repo_id (int) -> repo_data
                gh_repos_map = {r["id"]: r for r in github_repos}
                gh_repos_by_name = {r["name"].lower(): r for r in github_repos}

                # 3. Sync Logic: GitHub -> DB
                for r in github_repos:
                    repo_id = r["id"]
                    repo_name = r["name"]

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
                                f"Linking existing project '{project.name}' to GitHub ID {repo_id}"
                            )
                            project.github_repo_id = repo_id
                        else:
                            # Create new project
                            logger.info(f"Discovered new project: {repo_name} (ID: {repo_id})")
                            project = Project(
                                id=repo_name,  # Use name as ID for simplicity consistent with usage
                                name=repo_name,
                                github_repo_id=repo_id,
                                status=ProjectStatus.DISCOVERED.value,
                            )
                            db.add(project)

                    # Update metadata
                    # (Can update description, etc if we had those fields)

                    # Reset missing counter if it was missing
                    if project.id in missing_counters:
                        del missing_counters[project.id]
                        if project.status == ProjectStatus.MISSING.value:
                            project.status = (
                                ProjectStatus.ACTIVE.value
                            )  # Or INITIALIZED? Active implies deployed.
                            logger.info(f"Project {project.name} recovered from MISSING state.")

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
                            f"Project {proj.name} (ID: {proj.github_repo_id}) not found on GitHub. "
                            f"Attempt {count}/{MISSING_THRESHOLD}"
                        )

                        if count >= MISSING_THRESHOLD:
                            proj.status = ProjectStatus.MISSING.value
                            logger.error(
                                f"Marking project {proj.name} as MISSING after {count} failed checks."
                            )
                            # TODO: Send Alert

                await db.commit()
                logger.debug(f"Synced {len(github_repos)} repositories.")

        except Exception as e:
            logger.error(f"Error in GitHub Sync Worker: {e}", exc_info=True)

        await asyncio.sleep(SYNC_INTERVAL)

