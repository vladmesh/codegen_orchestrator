"""Scaffolder consumer — consumes from scaffold:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal

from pydantic import ValidationError
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.log_config import setup_logging
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.queues import SCAFFOLD_GROUP, SCAFFOLD_QUEUE
from shared.redis_client import RedisStreamClient
from src.clients.api import get_api_client
from src.clients.github import get_github_client
from src.config import get_settings
from src.scaffold import run_ensure_workspace, run_scaffold
from src.spec_extractor import extract_specs_summary

logger = structlog.get_logger(__name__)

_shutdown = False


def _handle_shutdown(signum, _frame):
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def process_scaffold_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single scaffold job.

    Args:
        job_data: Job data from Redis queue.
        redis: Redis client (unused but required by base worker signature).

    Returns:
        Result dict with status and details.
    """
    try:
        msg = ScaffoldMessage.model_validate(job_data)
    except ValidationError:
        logger.warning("scaffold_invalid_message", data=job_data)
        return {"status": "skipped", "error": "invalid message"}

    log = logger.bind(project_id=msg.project_id, repository_id=msg.repository_id)
    log.info("scaffold_job_started")

    api = get_api_client()
    settings = get_settings()

    try:
        # Get GitHub token
        github = get_github_client()
        org = os.environ.get("GITHUB_ORG", "")
        if not org:
            raise RuntimeError("GITHUB_ORG environment variable is not set")
        repo_full_name = f"{org}/{msg.project_name}"
        github_token = await github.get_org_token(org)

        # Route by mode
        args = (msg, repo_full_name, github, github_token, api, settings, log)
        if msg.mode == "ensure":
            return await _process_ensure_mode(*args)
        return await _process_full_mode(*args)

    except Exception as e:
        log.error("scaffold_job_exception", error=str(e), exc_info=True)
        return {"status": "failed", "error": str(e)}


async def _process_full_mode(msg, repo_full_name, github, github_token, api, settings, log) -> dict:
    """Full scaffold: create repo, copier, make setup, git push."""
    org = repo_full_name.split("/")[0]

    # Create GitHub repo (idempotent — ignores 422 if already exists)
    github_repo = None
    try:
        github_repo = await github.create_repo(org, msg.project_name, private=True)
    except Exception as e:
        if "422" not in str(e):
            raise

    # Update repository git_url + provider_repo_id so github_sync can match
    git_url = f"https://github.com/{repo_full_name}"
    update_fields: dict = {"git_url": git_url}
    if github_repo:
        update_fields["provider_repo_id"] = github_repo.id
    await api.update_repository(msg.repository_id, **update_fields)

    # Set registry secrets so CI build-and-push can work from first commit
    registry_url = os.environ.get("ORCHESTRATOR_HOSTNAME", "")
    registry_user = os.environ.get("REGISTRY_USER", "")
    registry_password = os.environ.get("REGISTRY_PASSWORD", "")
    if all([registry_url, registry_user, registry_password]):
        github_token_for_secrets = await github.get_org_token(org)
        count = await github.set_repository_secrets(
            org,
            msg.project_name,
            {
                "REGISTRY_URL": registry_url,
                "REGISTRY_USER": registry_user,
                "REGISTRY_PASSWORD": registry_password,
            },
            token=github_token_for_secrets,
        )
        log.info("registry_secrets_set", count=count)
    else:
        log.warning(
            "registry_secrets_skipped",
            has_url=bool(registry_url),
            has_user=bool(registry_user),
            has_password=bool(registry_password),
        )

    # Run scaffold
    result = await run_scaffold(
        project_id=msg.project_id,
        repository_id=msg.repository_id,
        template_repo=msg.template_repo,
        project_name=msg.project_name,
        modules=msg.modules,
        task_description=msg.task_description,
        repo_full_name=repo_full_name,
        github_token=github_token,
        settings=settings,
    )

    if result.success:
        await _update_project_on_success(msg, result, api, settings, log)

        # Set branch protection + auto-merge (non-fatal — scaffold succeeds regardless)
        try:
            await github.update_branch_protection(
                org,
                msg.project_name,
                "main",
                required_checks=["lint-and-test"],
                require_pr=True,
            )
            log.info("branch_protection_set")
        except Exception:
            log.warning("branch_protection_failed", exc_info=True)

        try:
            await github.enable_repo_auto_merge(org, msg.project_name)
            log.info("repo_auto_merge_enabled")
        except Exception:
            log.warning("repo_auto_merge_enable_failed", exc_info=True)

        await api.update_project_status(msg.project_id, ProjectStatus.ACTIVE)
        log.info("scaffold_job_success")
        return {"status": "success"}

    log.error("scaffold_job_failed", error=result.error)

    # Mark project so scaffold_trigger stops retrying every cycle
    try:
        project = await api.get_project(msg.project_id)
        config = dict(project.config) if project.config else {}
        config["scaffold_error"] = result.error or "unknown error"
        await api.update_project_config(msg.project_id, config)
    except Exception:
        log.warning("failed_to_mark_scaffold_error", exc_info=True)

    # Fail all stories so architect/dispatcher don't keep waiting
    try:
        stories = await api.get_stories_by_project(msg.project_id)
        for story in stories:
            await api.fail_story(story.id)
            log.info("scaffold_story_failed", story_id=story.id)
    except Exception:
        log.warning("failed_to_fail_stories_on_scaffold_error", exc_info=True)

    return {"status": "failed", "error": result.error or "unknown error"}


async def _process_ensure_mode(
    msg,
    repo_full_name,
    github,
    github_token,
    api,
    settings,
    log,
) -> dict:
    """Ensure workspace exists. Skip if present, clone+setup if missing."""
    org = repo_full_name.split("/")[0]

    # Check if repo exists on GitHub
    repo_exists = True
    try:
        await github.get_repo(org, msg.project_name)
    except Exception:
        repo_exists = False

    result = await run_ensure_workspace(
        repository_id=msg.repository_id,
        project_name=msg.project_name,
        repo_full_name=repo_full_name,
        github_token=github_token,
        settings=settings,
        repo_exists_on_github=repo_exists,
    )

    if result.skipped:
        log.info("ensure_workspace_skipped")
        return {"status": "skipped"}

    if result.success:
        await _update_project_on_success(msg, result, api, settings, log)
        log.info("ensure_workspace_success")
        return {"status": "success"}

    log.error("ensure_workspace_failed", error=result.error)

    # Mark project so scaffold_trigger stops retrying every cycle
    try:
        project = await api.get_project(msg.project_id)
        config = dict(project.config) if project.config else {}
        config["scaffold_error"] = result.error or "unknown error"
        await api.update_project_config(msg.project_id, config)
    except Exception:
        log.warning("failed_to_mark_scaffold_error")

    return {"status": "failed", "error": result.error or "unknown error"}


async def _update_project_on_success(msg, result, api, settings, log) -> None:
    """Update project config with tree and specs after successful scaffold/ensure."""
    workspace = Path(settings.workspace_base_path) / msg.repository_id
    project = await api.get_project(msg.project_id)
    config = dict(project.config) if project.config else {}
    config["tree"] = result.tree
    config["workspace_ready"] = True
    config.pop("scaffold_error", None)
    specs_summary = extract_specs_summary(workspace)
    if specs_summary:
        config["specs_summary"] = specs_summary
    await api.update_project_config(msg.project_id, config)


async def run_worker() -> None:
    """Run the scaffold queue consumer loop."""
    global _shutdown
    _shutdown = False

    setup_logging(service_name="scaffolder")
    consumer_name = f"scaffolder-{os.getpid()}"

    redis = RedisStreamClient()
    await redis.connect()

    logger.info("scaffolder_started", consumer=consumer_name)

    try:
        async for msg in redis.consume(
            SCAFFOLD_QUEUE,
            SCAFFOLD_GROUP,
            consumer_name,
            auto_ack=False,
            claim_pending=True,
        ):
            if _shutdown:
                break
            if msg is None:
                continue
            try:
                bind_message_context(msg.data)
                result = await process_scaffold_job(msg.data, redis)
                msg.data.update(result)
                await redis.ack(SCAFFOLD_QUEUE, SCAFFOLD_GROUP, msg.message_id)
                logger.debug("job_acked", entry_id=msg.message_id)
            except Exception as e:
                logger.error(
                    "job_processing_error",
                    entry_id=msg.message_id,
                    error=str(e),
                )
            finally:
                # Clear inflight marker so the scheduler can re-trigger if needed
                project_id = msg.data.get("project_id")
                if project_id:
                    inflight_key = f"scaffold:inflight:{project_id}"
                    await redis.redis.delete(inflight_key)
                unbind_message_context()
    finally:
        await redis.close()
        api = get_api_client()
        await api.close()
        logger.info("scaffolder_shutdown")


def main():
    """Entry point for running as module."""
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    asyncio.run(run_worker())
