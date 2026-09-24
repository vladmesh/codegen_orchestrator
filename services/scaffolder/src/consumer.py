"""Scaffolder consumer — consumes from scaffold:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import uuid

import httpx
from pydantic import ValidationError
import structlog

from shared.clients.github import (
    GitHubAppClient,
    RegistrySecretsNotRefreshedError,
    registry_repository_secrets,
)
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode, in_work_cycle
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.diagnostics import redact_diagnostic, safe_validation_errors
from shared.log_config import setup_logging
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.notifications import notify_admins_best_effort
from shared.queues import SCAFFOLD_GROUP, SCAFFOLD_QUEUE
from shared.redis import RedisStreamClient
from src.clients.api import get_api_client
from src.config import get_settings
from src.scaffold import run_ensure_workspace, run_scaffold
from src.spec_extractor import extract_specs_summary

logger = structlog.get_logger(__name__)

_shutdown = False


SCAFFOLD_LEASE_SECONDS = 900
SCAFFOLD_LEASE_REFRESH_SECONDS = 300


def scaffold_leases_key(project_id: str) -> str:
    return f"live:scaffold:leases:{project_id}"


def scaffold_cancel_key(project_id: str) -> str:
    return f"live:scaffold:cancelled:{project_id}"


async def _begin_scaffold_work(redis: RedisStreamClient, project_id: str) -> str | None:
    """Atomically register one execution lease unless teardown has cancelled it."""
    token = uuid.uuid4().hex
    registered = await redis.redis.eval(
        """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        local now = redis.call('TIME')
        local expires = now[1] * 1000 + math.floor(now[2] / 1000) + ARGV[2] * 1000
        redis.call('ZADD', KEYS[2], expires, ARGV[1])
        redis.call('EXPIRE', KEYS[2], ARGV[2] * 2)
        return 1
        """,
        2,
        scaffold_cancel_key(project_id),
        scaffold_leases_key(project_id),
        token,
        SCAFFOLD_LEASE_SECONDS,
    )
    return token if registered == 1 else None


async def _refresh_scaffold_lease(redis: RedisStreamClient, project_id: str, token: str) -> None:
    while True:
        await asyncio.sleep(SCAFFOLD_LEASE_REFRESH_SECONDS)
        refreshed = await redis.redis.eval(
            """
            if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then return 0 end
            local now = redis.call('TIME')
            local expires = now[1] * 1000 + math.floor(now[2] / 1000) + ARGV[2] * 1000
            redis.call('ZADD', KEYS[1], 'XX', expires, ARGV[1])
            redis.call('EXPIRE', KEYS[1], ARGV[2] * 2)
            return 1
            """,
            1,
            scaffold_leases_key(project_id),
            token,
            SCAFFOLD_LEASE_SECONDS,
        )
        if refreshed == 0:
            raise RuntimeError("scaffold execution lease expired")


async def _finish_scaffold_work(redis: RedisStreamClient, project_id: str, token: str) -> None:
    await redis.redis.zrem(scaffold_leases_key(project_id), token)


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
    except ValidationError as exc:
        logger.warning("scaffold_invalid_message", errors=safe_validation_errors(exc))
        return {"status": "skipped", "error": "invalid message"}

    log = logger.bind(project_id=msg.project_id, repository_id=msg.repository_id)
    log.info("scaffold_job_started")

    lease = await _begin_scaffold_work(redis, msg.project_id)
    if lease is None:
        log.info("scaffold_job_cancelled_by_live_teardown")
        return {"status": "skipped", "error": "cancelled by live teardown"}
    lease_refresh = asyncio.create_task(_refresh_scaffold_lease(redis, msg.project_id, lease))
    owner_task = asyncio.current_task()

    def cancel_work_on_lost_lease(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None and owner_task is not None:
            owner_task.cancel()

    lease_refresh.add_done_callback(cancel_work_on_lost_lease)

    api = get_api_client()
    settings = get_settings()

    try:
        # One GitHub HTTP pool spans every GitHub call of this operation and is
        # closed on success, failure and cancellation alike.
        async with GitHubAppClient() as github:
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

    except Exception as exc:
        error = redact_diagnostic(exc)
        log.error("scaffold_job_exception", error=error, exc_info=True)
        if msg.mode == "ensure":
            # An exception is an ensure failure like any other: recorded, so the
            # API parks the project's stories instead of refusing them silently.
            await _record_scaffold_error(msg, error, api, log)
        return {"status": "failed", "error": error}
    finally:
        lease_refresh.cancel()
        with suppress(asyncio.CancelledError):
            await lease_refresh
        await _finish_scaffold_work(redis, msg.project_id, lease)


async def _process_full_mode(msg, repo_full_name, github, github_token, api, settings, log) -> dict:
    """Full scaffold: create repo, copier, make setup, git push."""
    org = repo_full_name.split("/")[0]

    # Create GitHub repo. A 422 may be an idempotent existing-repository case;
    # verify it with a read before continuing. Every other failure propagates.
    try:
        github_repo = await github.create_repo(org, msg.project_name, private=True)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
            raise
        try:
            github_repo = await github.get_repo(org, msg.project_name)
        except httpx.HTTPStatusError as lookup_exc:
            if lookup_exc.response.status_code == httpx.codes.NOT_FOUND:
                raise exc from lookup_exc
            raise

    # Update repository git_url + provider_repo_id so github_sync can match
    git_url = f"https://github.com/{repo_full_name}"
    update_fields: dict = {"git_url": git_url}
    if github_repo:
        update_fields["provider_repo_id"] = github_repo.id
    await api.update_repository(msg.repository_id, **update_fields)

    # Set registry secrets so CI build-and-push can work from first commit. The PR
    # poller writes them again before every merge, so this is not the only chance.
    try:
        registry_secrets = registry_repository_secrets()
    except RegistrySecretsNotRefreshedError as error:
        log.warning("registry_secrets_skipped", detail=error.detail)
    else:
        github_token_for_secrets = await github.get_org_token(org)
        count = await github.set_repository_secrets(
            org, msg.project_name, registry_secrets, token=github_token_for_secrets
        )
        log.info("registry_secrets_set", count=count)

    # Run scaffold
    result = await run_scaffold(
        project_id=msg.project_id,
        repository_id=msg.repository_id,
        template_repo=msg.template_repo,
        template_ref=msg.template_ref,
        project_name=msg.project_name,
        modules=msg.modules,
        task_description=msg.task_description,
        repo_full_name=repo_full_name,
        github_token=github_token,
        settings=settings,
    )

    if result.success:
        project_config = await _update_project_on_success(msg, result, api, settings, log)

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

        await _verify_repo_auto_merge(msg, github, api, org, project_config, log)

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

    await _fail_stories_waiting_on_scaffold(msg, result.error or "unknown error", api, log)
    return {"status": "failed", "error": result.error or "unknown error"}


async def _fail_stories_waiting_on_scaffold(msg, error: str, api, log) -> None:
    """Fail every story whose only work so far was waiting for this scaffold.

    That is a story still in ``created``, and one an architect already took to
    ``in_progress`` while it waited for the repository but that has no task in
    its current work cycle: nothing was built for it, and nothing will be,
    because ``scaffold_trigger`` never retries a project carrying
    ``scaffold_error``. Leaving it in ``in_progress`` is what made its owner hear
    "work continues" for as long as anybody asked.

    Work that has tasks, or already left ``in_progress`` (review, deploy,
    testing, waiting_*), or finished (completed, archived) is not defective
    because this scaffold run failed, and failing it destroys user-visible state
    that nothing rolls back. Each failure carries the typed reason, so the story
    itself says why it stopped and its owner is owed the cause.
    """
    failure = StoryFailure(code=StoryFailureCode.SCAFFOLD_FAILED, source="scaffolder", detail=error)
    try:
        stories = await api.get_stories_by_project(msg.project_id)
        failed_ids = []
        skipped_ids = []
        for story in stories:
            if not await _waits_only_on_scaffold(story, api):
                skipped_ids.append(story.id)
                continue
            await api.fail_story(story.id, failure)
            failed_ids.append(story.id)
            log.info("scaffold_story_failed", story_id=story.id, story_status=story.status)
        log.info(
            "scaffold_stories_failed_summary",
            failed_count=len(failed_ids),
            failed_story_ids=failed_ids,
            skipped_count=len(skipped_ids),
            skipped_story_ids=skipped_ids,
        )
    except Exception:
        log.warning("failed_to_fail_stories_on_scaffold_error", exc_info=True)


async def _waits_only_on_scaffold(story, api) -> bool:
    if story.status == StoryStatus.CREATED:
        return True
    if story.status != StoryStatus.IN_PROGRESS:
        return False
    tasks = await api.get_tasks_by_story(story.id)
    return not any(in_work_cycle(task.created_at, story.reopened_at, task.status) for task in tasks)


async def _verify_repo_auto_merge(msg, github, api, org, project_config, log) -> None:
    """Prove GitHub accepted repository auto-merge, or leave an actionable record."""
    try:
        await github.enable_repo_auto_merge(org, msg.project_name)
        repository = await github.get_repo(org, msg.project_name)
        if getattr(repository, "allow_auto_merge", None) is not True:
            raise RuntimeError("GitHub read-back reported allow_auto_merge=false")
        if "repo_auto_merge_verification" in project_config:
            project_config.pop("repo_auto_merge_verification")
            await api.update_project_config(msg.project_id, project_config)
        log.info("repo_auto_merge_verified")
    except Exception as exc:
        error = redact_diagnostic(exc)
        log.error("repo_auto_merge_verification_failed", error=error, exc_info=True)
        project_config["repo_auto_merge_verification"] = {"status": "failed", "error": error}
        try:
            await api.update_project_config(msg.project_id, project_config)
        except Exception:
            log.exception("repo_auto_merge_failure_mark_write_failed")
        await notify_admins_best_effort(
            f"Repository {org}/{msg.project_name} did not enable auto-merge: {error}",
            level="error",
            project_id=msg.project_id,
            repository_id=msg.repository_id,
        )


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

    # Only a GitHub 404 means the repository is absent. Authentication,
    # transport and server failures must not be converted into "does not exist".
    repo_exists = True
    try:
        await github.get_repo(org, msg.project_name)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.NOT_FOUND:
            raise
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
    await _record_scaffold_error(msg, result.error or "unknown error", api, log)
    return {"status": "failed", "error": result.error or "unknown error"}


async def _record_scaffold_error(msg, error: str, api, log) -> None:
    """Record a failed ensure on the project.

    `scaffold_error` stops scaffold_trigger re-running ensure every cycle, and
    the API's dispatch admission parks each story with a todo task on it. The
    operator's infrastructure retry is what removes it.
    """
    try:
        project = await api.get_project(msg.project_id)
        config = dict(project.config) if project.config else {}
        config["scaffold_error"] = error
        await api.update_project_config(msg.project_id, config)
    except Exception:
        log.warning("failed_to_mark_scaffold_error", exc_info=True)


async def _update_project_on_success(msg, result, api, settings, log) -> dict:
    """Update project config with tree and specs after successful scaffold/ensure."""
    workspace = Path(settings.workspace_base_path) / msg.repository_id
    project = await api.get_project(msg.project_id)
    config = dict(project.config) if project.config else {}
    config["tree"] = result.tree
    config["workspace_ready"] = True
    if result.template_commit:
        config["service_template"] = {
            "source": msg.template_repo,
            "requested_ref": msg.template_ref,
            "commit": result.template_commit,
        }
    config.pop("scaffold_error", None)
    specs_summary = extract_specs_summary(workspace)
    if specs_summary:
        config["specs_summary"] = specs_summary
    await api.update_project_config(msg.project_id, config)
    return config


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
