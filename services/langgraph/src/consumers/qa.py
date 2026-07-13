"""QA Worker — consumes from qa:queue and runs post-deploy QA testing.

Pure technical worker: only updates run.status and run.result.
Story lifecycle (TESTING → COMPLETED/FAILED) is managed by the dispatcher's
supervise_testing_stories(), which reads run.result.qa_outcome.

Run standalone: python -m src.consumers.qa
"""

from __future__ import annotations

import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import QAFailedCheck, QARunResult
from shared.contracts.queues.qa import QAMessage, QAOutcome, QAServerInfo
from shared.queues import QA_GROUP, QA_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._base import run_queue_worker, validate_queued_message
from ._qa_runner import QAResult, credential_refresh_loop, run_qa_on_server

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
        redis: Redis client for inflight markers

    Returns:
        Result dict with status and details
    """
    msg = validate_queued_message(QAMessage, job_data)
    story_id = msg.story_id
    run_id = msg.run_id

    logger.info(
        "qa_job_started",
        story_id=story_id or None,
        application_id=msg.application_id,
        qa_attempt=msg.qa_attempt,
    )

    # Inflight dedup — prevent concurrent QA on same story/application
    dedup_id = story_id if story_id else str(msg.application_id)
    inflight_key = f"qa:inflight:{dedup_id}"
    acquired = await redis.redis.set(inflight_key, "1", nx=True, ex=QA_INFLIGHT_TTL)
    if not acquired:
        logger.info("qa_already_inflight", dedup_id=dedup_id)
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
            await _update_run(run_id, RunStatus.FAILED, QAOutcome.ERROR, error=error)
            return {"status": "error", "error": error}

        # Fail-fast: if project has tg_bot module, bot_username is required
        if not msg.bot_username:
            project = await api_client.get_project(msg.project_id)
            modules = (project.config or {}).get("modules", [])
            if "tg_bot" in modules:
                error = (
                    "Project has tg_bot module but bot_username is missing in QAMessage. "
                    "Deploy smoke test should have resolved it via getMe."
                )
                logger.error("qa_bot_username_missing", story_id=story_id, modules=modules)
                await _update_run(run_id, RunStatus.FAILED, QAOutcome.ERROR, error=error)
                return {"status": "error", "error": error}

        # Resolve acceptance criteria from repository
        app = await api_client.get_application(msg.application_id)
        repo = await api_client.get_repository(app.repo_id)
        acceptance_criteria = repo.acceptance_criteria or ""

        if not acceptance_criteria:
            error = (
                f"Repository {repo.id} has no acceptance_criteria. "
                "Cannot run QA without regression test criteria."
            )
            logger.error("qa_no_acceptance_criteria", repo_id=repo.id)
            await _update_run(run_id, RunStatus.FAILED, QAOutcome.ERROR, error=error)
            return {"status": "error", "error": error}

        # Mark run as running before starting Claude Code
        if run_id:
            await api_client.patch(
                f"runs/{run_id}",
                json={"status": RunStatus.RUNNING.value},
            )

        # Run QA on server
        qa_result = await run_qa_on_server(
            server_ip=server_info.server_ip,
            ssh_key=server_info.ssh_key,
            project_name=server_info.project_name,
            acceptance_criteria=acceptance_criteria,
            deployed_url=msg.deployed_url,
            bot_username=msg.bot_username,
        )

        logger.info(
            "qa_result",
            story_id=story_id,
            passed=qa_result.passed,
            summary=qa_result.summary,
            checks_count=len(qa_result.checks),
            has_report=bool(qa_result.report),
        )

        # Log the full QA report for observability
        if qa_result.report:
            logger.info(
                "qa_report_content",
                story_id=story_id,
                report=qa_result.report[:2000],
            )

        if qa_result.passed:
            return await _handle_qa_pass(
                run_id=run_id,
                deployed_url=msg.deployed_url,
                report=qa_result.report,
            )
        else:
            return await _handle_qa_fail(
                run_id=run_id,
                qa_attempt=msg.qa_attempt,
                qa_result=qa_result,
            )

    finally:
        # Always release inflight marker
        await redis.redis.delete(inflight_key)


async def _handle_qa_pass(*, run_id: str, deployed_url: str, report: str = "") -> dict:
    """Handle QA pass — store PASSED outcome in run."""
    await _update_run(
        run_id,
        RunStatus.COMPLETED,
        QAOutcome.PASSED,
        deployed_url=deployed_url,
        report=report,
    )
    logger.info("qa_passed", run_id=run_id)
    return {"status": "passed"}


async def _handle_qa_fail(
    *,
    run_id: str,
    qa_attempt: int,
    qa_result: QAResult,
) -> dict:
    """Handle QA fail — store FAILED or EXHAUSTED outcome in run."""
    failed_checks = [
        QAFailedCheck(name=c.get("name", ""), detail=c.get("detail", ""))
        for c in qa_result.checks
        if not c.get("pass", True)
    ]

    if qa_attempt >= MAX_QA_LOOPS:
        logger.warning(
            "qa_loops_exhausted",
            run_id=run_id,
            attempt=qa_attempt,
            max_loops=MAX_QA_LOOPS,
        )
        await _update_run(
            run_id,
            RunStatus.COMPLETED,
            QAOutcome.EXHAUSTED,
            summary=qa_result.summary,
            failed_checks=failed_checks,
            qa_attempt=qa_attempt,
            report=qa_result.report,
        )
        return {"status": "qa_exhausted"}

    await _update_run(
        run_id,
        RunStatus.COMPLETED,
        QAOutcome.FAILED,
        summary=qa_result.summary,
        failed_checks=failed_checks,
        qa_attempt=qa_attempt,
        report=qa_result.report,
    )

    logger.info(
        "qa_failed",
        run_id=run_id,
        attempt=qa_attempt,
    )
    return {"status": "qa_failed"}


async def _update_run(
    run_id: str,
    status: RunStatus,
    qa_outcome: QAOutcome,
    **extra_result: object,
) -> None:
    """Update run status and result with QA outcome."""
    if not run_id:
        logger.warning("qa_no_run_id_skip_update")
        return
    run_result = QARunResult(qa_outcome=qa_outcome, **extra_result)
    await api_client.patch(
        f"runs/{run_id}",
        json={
            "status": status.value,
            "result": run_result.model_dump(mode="json"),
        },
    )


def main():
    """Entry point for running as module.

    Runs the queue consumer and credential refresh loop concurrently.
    The refresh loop keeps OAuth tokens fresh on all managed servers,
    preventing token expiry between QA runs.
    """
    import asyncio
    import signal

    from ._base import _handle_shutdown

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    async def _run():
        refresh_task = asyncio.create_task(
            credential_refresh_loop(),
            name="credential_refresh",
        )
        worker_task = asyncio.create_task(
            run_queue_worker("qa-worker", QA_QUEUE, process_qa_job, group=QA_GROUP),
            name="qa_consumer",
        )
        done, pending = await asyncio.wait(
            [refresh_task, worker_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
