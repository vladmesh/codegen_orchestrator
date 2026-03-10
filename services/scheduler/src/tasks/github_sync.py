"""GitHub sync worker - syncs projects and their status from GitHub."""

import asyncio
import hashlib
import hmac
import json
import os
import time

import httpx
from pydantic import ValidationError
import structlog
import yaml

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.project import ProjectDTO, ProjectStatus, ProjectUpdate
from shared.notifications import notify_admins
from shared.schemas.github import GitHubRepository
from shared.schemas.project_spec import ProjectSpecYAML
from src.clients.api import api_client
from src.config import get_settings

logger = structlog.get_logger()

# Config
SYNC_INTERVAL = 300  # 5 minutes
MISSING_THRESHOLD = 3  # Alert after 3 consecutive checks where repo is missing


async def _ingest_to_rag(
    project_id: str,
    repo_full_name: str,
    documents: list[dict],
) -> None:
    """Send documents to RAG ingest API (best-effort, non-blocking).

    Documents are indexed with hash-based deduplication - unchanged
    content will be skipped by the API automatically.
    """
    settings = get_settings()
    secret = os.getenv("RAG_INGEST_SECRET")

    if not settings.api_base_url or not secret:
        logger.debug(
            "rag_ingest_skipped",
            reason="missing_config",
            has_api_url=bool(settings.api_base_url),
            has_secret=bool(secret),
        )
        return

    if not documents:
        return

    # Build payload
    payload = {
        "event": "rag.docs.upsert",
        "project_id": project_id,
        "documents": documents,
    }

    # Build HMAC signature
    timestamp = int(time.time())
    body = json.dumps(payload).encode("utf-8")
    message = f"{timestamp}.".encode() + body
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-RAG-Timestamp": str(timestamp),
        "X-RAG-Signature": f"sha256={signature}",
    }

    try:
        result = await api_client.ingest_rag(body=body, headers=headers)

        logger.info(
            "rag_ingest_success",
            project_id=project_id,
            repo=repo_full_name,
            docs_received=result.get("documents_received", 0),
            docs_indexed=result.get("documents_indexed", 0),
            docs_skipped=result.get("documents_skipped", 0),
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "rag_ingest_http_error",
            project_id=project_id,
            repo=repo_full_name,
            status_code=exc.response.status_code,
            detail=exc.response.text[:200],
        )
    except Exception as exc:
        logger.warning(
            "rag_ingest_failed",
            project_id=project_id,
            repo=repo_full_name,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _hash_content(content: str) -> str:
    """Generate SHA256 hash of content for deduplication."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


async def _sync_project_docs(
    github_client: GitHubAppClient,
    project: ProjectDTO,
    r: GitHubRepository,
) -> None:
    """Sync project spec and README to RAG index."""
    rag_documents: list[dict] = []
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
                project_name=project.name,
                spec_version=spec_dict.get("version", "unknown"),
            )
            rag_documents.append(
                {
                    "source_type": "project_spec",
                    "source_id": ".project-spec.yaml",
                    "source_uri": f"repo://{r.full_name}/.project-spec.yaml",
                    "scope": "public",
                    "path": ".project-spec.yaml",
                    "title": f"{project.name} Project Spec",
                    "content": spec_content,
                    "content_hash": _hash_content(spec_content),
                }
            )
    except ValidationError as e:
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
        logger.debug(
            "project_spec_sync_skipped",
            project_name=project.name,
            error=str(e),
            error_type=type(e).__name__,
        )

    # Fetch README.md for RAG
    try:
        readme_content = await github_client.get_file_contents(owner, repo, "README.md")
        if readme_content:
            rag_documents.append(
                {
                    "source_type": "readme",
                    "source_id": "README.md",
                    "source_uri": f"repo://{r.full_name}/README.md",
                    "scope": "public",
                    "path": "README.md",
                    "title": f"{project.name} README",
                    "content": readme_content,
                    "content_hash": _hash_content(readme_content),
                }
            )
    except Exception as e:
        logger.debug(
            "readme_fetch_skipped",
            project_name=project.name,
            error=str(e),
            error_type=type(e).__name__,
        )

    # Ingest documents to RAG (best-effort)
    if rag_documents:
        await _ingest_to_rag(
            project_id=str(project.id),
            repo_full_name=r.full_name,
            documents=rag_documents,
        )


async def _sync_single_repo(
    github_client: GitHubAppClient,
    r: GitHubRepository,
    missing_counters: dict[str, int],
) -> None:
    """Sync a single repository to the database and RAG index."""
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
        await notify_admins(
            f"⚠️ Repository *{repo_name}* (GitHub ID: {repo_id}) "
            "found in org but has no matching repository in DB. "
            "Create it manually if needed.",
            level="warning",
        )
        return

    project_id = db_repo.get("project_id")
    project = await api_client.get_project(str(project_id)) if project_id else None
    if not project:
        logger.warning("repo_orphaned", repo_name=repo_name, project_id=project_id)
        return

    # Sync project spec and README to RAG
    await _sync_project_docs(github_client, project, r)

    # Reset missing counter if it was missing
    project_id_str = str(project.id)
    if project_id_str in missing_counters:
        del missing_counters[project_id_str]
        if project.status == ProjectStatus.MISSING:
            await api_client.update_project(
                project_id_str, ProjectUpdate(status=ProjectStatus.ACTIVE)
            )
            logger.info(
                "project_recovered",
                project_name=project.name,
                provider_repo_id=repo_id,
            )


async def _detect_missing_projects(
    gh_repos_map: dict[int, GitHubRepository],
    missing_counters: dict[str, int],
) -> None:
    """Detect and alert on projects missing from GitHub."""
    db_projects = await api_client.get_projects()

    # For each active project, check if its repositories are present on GitHub
    active_projects = [
        p for p in db_projects if p.status not in (ProjectStatus.MISSING, ProjectStatus.ARCHIVED)
    ]

    for proj in active_projects:
        project_id_str = str(proj.id)
        repos = await api_client.get_repositories(project_id=project_id_str)
        managed_repos = [r for r in repos if r.get("provider_repo_id") is not None]

        if not managed_repos:
            continue  # No repos with provider_repo_id to check

        # Check if any managed repo is missing from GitHub
        all_present = all(r.get("provider_repo_id") in gh_repos_map for r in managed_repos)

        if not all_present:
            count = missing_counters.get(project_id_str, 0) + 1
            missing_counters[project_id_str] = count

            logger.warning(
                "project_missing_from_github",
                project_name=proj.name,
                project_id=project_id_str,
                attempt=count,
                threshold=MISSING_THRESHOLD,
            )

            if count >= MISSING_THRESHOLD:
                await api_client.update_project(
                    project_id_str, ProjectUpdate(status=ProjectStatus.MISSING)
                )
                logger.error(
                    "project_marked_missing",
                    project_name=proj.name,
                    project_id=project_id_str,
                    attempts=count,
                )
                await notify_admins(
                    f"🚨 Project *{proj.name}* is MISSING! "
                    f"Repository not found after {count} consecutive checks.",
                    level="critical",
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
                await asyncio.sleep(SYNC_INTERVAL)
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
                await asyncio.sleep(SYNC_INTERVAL)
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

        await asyncio.sleep(SYNC_INTERVAL)
