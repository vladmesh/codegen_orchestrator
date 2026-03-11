"""Scaffolder consumer — consumes from scaffold:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import os
import signal

from pydantic import ValidationError
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.log_config import setup_logging
from shared.queues import SCAFFOLD_GROUP, SCAFFOLD_QUEUE
from shared.redis_client import RedisStreamClient
from src.clients.api import get_api_client
from src.clients.github import get_github_client
from src.config import get_settings
from src.scaffold import run_scaffold

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
        # Set status to scaffolding
        await api.update_project_status(msg.project_id, ProjectStatus.SCAFFOLDING)

        # Get GitHub token
        github = get_github_client()
        org = os.environ.get("GITHUB_ORG", "")
        if not org:
            raise RuntimeError("GITHUB_ORG environment variable is not set")
        repo_full_name = f"{org}/{msg.project_name}"

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

        github_token = await github.get_org_token(org)

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
            # Save tree to project config
            project_data = await api.get_project(msg.project_id)
            config = project_data.get("config", {}) or {}
            config["tree"] = result.tree
            await api.update_project_config(msg.project_id, config)

            # Set status to scaffolded
            await api.update_project_status(msg.project_id, ProjectStatus.SCAFFOLDED)
            log.info("scaffold_job_success")
            return {"status": "success"}
        else:
            await api.update_project_status(msg.project_id, ProjectStatus.SCAFFOLD_FAILED)
            log.error("scaffold_job_failed", error=result.error)
            return {"status": "failed", "error": result.error or "unknown error"}

    except Exception as e:
        log.error("scaffold_job_exception", error=str(e), exc_info=True)
        try:
            await api.update_project_status(msg.project_id, ProjectStatus.SCAFFOLD_FAILED)
        except Exception:
            log.error("scaffold_status_update_failed_on_error")
        return {"status": "failed", "error": str(e)}


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
        await redis.close()
        api = get_api_client()
        await api.close()
        logger.info("scaffolder_shutdown")


def main():
    """Entry point for running as module."""
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    asyncio.run(run_worker())
