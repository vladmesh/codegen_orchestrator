"""QA Worker — consumes from qa:queue and runs post-deploy QA testing.

After deploy+smoke succeed, the QA consumer SSHes to the prod server,
runs Claude Code with a QA prompt built from the story description,
and routes the result: pass → complete story, fail → create fix task.

Run standalone: python -m src.consumers.qa
"""

from __future__ import annotations

import structlog

from shared.contracts.queues.qa import QAMessage, QAServerInfo
from shared.queues import QA_GROUP, QA_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._base import start_worker
from ._events import publish_story_event
from ._qa_runner import QAResult, run_qa_on_server

logger = structlog.get_logger(__name__)

MAX_QA_LOOPS = 2  # max QA→Engineering cycles before story is marked failed
QA_INFLIGHT_TTL = 1500  # 25 min TTL for inflight marker


async def _resolve_server_info(application_id: int) -> QAServerInfo | None:
    """Resolve server IP, SSH key, and project name from application_id.

    Returns:
        QAServerInfo with connection details, or None on failure.
    """
    try:
        app = await api_client.get_application(application_id)
    except Exception:
        logger.warning("qa_application_not_found", application_id=application_id, exc_info=True)
        return None

    if not app.server_handle:
        logger.warning("qa_no_server_handle", application_id=application_id)
        return None

    server = await api_client.get_server(app.server_handle)
    ssh_key = await api_client.get_server_ssh_key(app.server_handle)

    if not server.public_ip or not ssh_key:
        logger.warning(
            "qa_server_incomplete",
            application_id=application_id,
            has_ip=bool(server.public_ip),
            has_ssh_key=bool(ssh_key),
        )
        return None

    return QAServerInfo(
        server_ip=server.public_ip,
        ssh_key=ssh_key,
        project_name=app.service_name,
    )


async def process_qa_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single QA job from qa:queue.

    Args:
        job_data: Job data from Redis queue (QAMessage fields)
        redis: Redis client for publishing events and inflight markers

    Returns:
        Result dict with status and details
    """
    msg = QAMessage.model_validate(job_data)
    story_id = msg.story_id
    user_id = msg.user_id

    logger.info(
        "qa_job_started",
        story_id=story_id,
        application_id=msg.application_id,
        qa_attempt=msg.qa_attempt,
    )

    # Inflight dedup — prevent concurrent QA on same story
    inflight_key = f"qa:inflight:{story_id}"
    acquired = await redis.redis.set(inflight_key, "1", nx=True, ex=QA_INFLIGHT_TTL)
    if not acquired:
        logger.info("qa_already_inflight", story_id=story_id)
        return {"status": "skipped", "reason": "already_inflight"}

    try:
        # Resolve server info
        server_info = await _resolve_server_info(msg.application_id)
        if not server_info:
            error = f"Cannot resolve server for application {msg.application_id}"
            logger.error(
                "qa_server_resolve_failed",
                application_id=msg.application_id,
            )
            return {"status": "error", "error": error}

        # Fetch story description for QA prompt
        story = await api_client.get_story(story_id)
        story_description = story.description or ""

        # Run QA on server
        qa_result = await run_qa_on_server(
            server_ip=server_info.server_ip,
            ssh_key=server_info.ssh_key,
            project_name=server_info.project_name,
            story_description=story_description,
            deployed_url=msg.deployed_url,
            bot_username=msg.bot_username,
        )

        logger.info(
            "qa_result",
            story_id=story_id,
            passed=qa_result.passed,
            summary=qa_result.summary,
            checks_count=len(qa_result.checks),
        )

        if qa_result.passed:
            return await _handle_qa_pass(
                story_id=story_id,
                user_id=user_id,
                deployed_url=msg.deployed_url,
                project_name=server_info.project_name,
                redis=redis,
            )
        else:
            return await _handle_qa_fail(
                msg=msg,
                qa_result=qa_result,
                project_name=server_info.project_name,
                redis=redis,
            )

    finally:
        # Always release inflight marker
        await redis.redis.delete(inflight_key)


async def _handle_qa_pass(
    *,
    story_id: str,
    user_id: str,
    deployed_url: str,
    project_name: str,
    redis: RedisStreamClient,
) -> dict:
    """Handle QA pass — complete story, notify user."""
    await _transition_story_safe(story_id, "complete")

    await publish_story_event(
        redis,
        user_id=user_id,
        event="story_completed",
        text=f"QA passed. Project '{project_name}' is live at {deployed_url}",
    )

    logger.info("qa_passed", story_id=story_id)
    return {"status": "passed"}


async def _handle_qa_fail(
    *,
    msg: QAMessage,
    qa_result: QAResult,
    project_name: str,
    redis: RedisStreamClient,
) -> dict:
    """Handle QA fail — create fix task or fail story if retries exhausted."""
    story_id = msg.story_id
    user_id = msg.user_id
    attempt = msg.qa_attempt

    if attempt >= MAX_QA_LOOPS:
        logger.warning(
            "qa_loops_exhausted",
            story_id=story_id,
            attempt=attempt,
            max_loops=MAX_QA_LOOPS,
        )
        await _transition_story_safe(story_id, "fail")
        await publish_story_event(
            redis,
            user_id=user_id,
            event="story_failed",
            text=(
                f"QA failed after {attempt} fix attempts for '{project_name}'. "
                f"Last issue: {qa_result.summary}"
            ),
        )
        return {"status": "qa_exhausted"}

    # Build fix task description from QA checks
    failed_checks = [c for c in qa_result.checks if not c.get("pass", True)]
    issues_text = "\n".join(
        f"- {c.get('name', 'unknown')}: {c.get('detail', 'failed')}" for c in failed_checks
    )
    if not issues_text:
        issues_text = qa_result.summary or "QA testing failed"

    fix_description = (
        f"QA testing found issues after deploy. Fix the following:\n\n"
        f"{issues_text}\n\n"
        f"QA summary: {qa_result.summary}"
    )

    await api_client.create_task(
        {
            "project_id": msg.project_id,
            "story_id": story_id,
            "title": f"QA fix: {qa_result.summary[:80]}",
            "type": "fix",
            "status": "todo",
            "description": fix_description,
        }
    )

    await _transition_story_safe(story_id, "start")

    logger.info(
        "qa_fix_task_created",
        story_id=story_id,
        attempt=attempt + 1,
    )
    return {"status": "qa_failed"}


async def _transition_story_safe(story_id: str, action: str) -> None:
    """Transition story status, logging errors without raising."""
    if not story_id:
        return
    try:
        await api_client.transition_story(story_id, action)
        logger.info("story_transitioned", story_id=story_id, action=action)
    except Exception:
        logger.warning("story_transition_failed", story_id=story_id, action=action, exc_info=True)


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="qa-worker",
        queue=QA_QUEUE,
        process_fn=process_qa_job,
        group=QA_GROUP,
    )


if __name__ == "__main__":
    main()
