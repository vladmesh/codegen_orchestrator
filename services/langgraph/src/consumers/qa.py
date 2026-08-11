"""QA Worker — consumes from qa:queue and runs post-deploy QA testing.

Pure technical worker: only updates run.status and run.result.
Story lifecycle (TESTING → COMPLETED/FAILED) is managed by the dispatcher's
supervise_testing_stories(), which reads run.result.qa_outcome.

Run standalone: python -m src.consumers.qa
"""

from __future__ import annotations

import httpx
import structlog

from shared.contracts.acceptance import parse_health_only_criteria
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory, QAFailedCheck, QARunResult
from shared.contracts.queues.qa import QAMessage, QAOutcome, QAServerInfo
from shared.queues import QA_GROUP, QA_QUEUE
from shared.redis_client import RedisStreamClient
from shared.telegram_access_probe import TelethonCredentialsError, telethon_env

from ..clients.api import api_client
from ..config.agent_llm_env import missing_llm_env
from ..config.settings import get_settings
from ..runtime_identity import project_runtime_slug
from ._base import run_queue_worker, validate_queued_message
from ._live_work import live_work_settled
from ._qa_runner import (
    QAResult,
    QARuntimeConfig,
    check_deployed_url_reachable,
    preflight_bot_access,
    run_health_checks,
    run_qa_centrally,
)
from ._qa_target import QATarget

logger = structlog.get_logger(__name__)

MAX_QA_LOOPS = 2  # max QA→Engineering cycles before story is marked failed
QA_INFLIGHT_TTL = 1500  # 25 min TTL for inflight marker


async def _resolve_server_info(application_id: int, project_name: str) -> QAServerInfo | None:
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
        ssh_user=server.ssh_user,
        ssh_key=ssh_key,
        project_name=project_name,
    )


def _resolve_qa_runtime() -> tuple[QARuntimeConfig | None, QABlocker | None]:
    """Build the central QA runtime config, or say what it is missing.

    Both halves live in this service's environment now: the LLM the QA agent
    thinks with, and the Telegram account it talks to bots as. Neither is ever
    written to a deploy target. A missing Telethon setup is not fatal here — a
    deployment without a bot never needs it — so it is reported by the bot
    preflight instead, which is the only place it matters.
    """
    settings = get_settings()
    missing = missing_llm_env("qa", settings)
    if missing:
        return None, QABlocker(
            category=QABlockerCategory.CLAUDE_UNAVAILABLE,
            attempted="start the central QA agent",
            sent=", ".join(missing),
            received="the QA runtime has no LLM configured, so no agent can run",
        )
    try:
        credentials = telethon_env()
    except TelethonCredentialsError as exc:
        logger.info("qa_telethon_not_configured", detail=str(exc))
        credentials = None
    return (
        QARuntimeConfig(
            model=settings.qa_llm_model,
            base_url=settings.qa_llm_base_url,
            api_key=settings.qa_llm_api_key,
            telethon_env=credentials,
        ),
        None,
    )


async def _run_exploratory_qa(
    *,
    msg: QAMessage,
    server_info: QAServerInfo,
    acceptance_criteria: str,
) -> tuple[QAResult | None, QABlocker | None]:
    """Run the central QA agent against one deployment.

    Returns either a product verdict or the blocker that stopped QA from
    reaching one. Everything the platform owes the run — an LLM to think with, a
    Telegram account that the bot admits — is settled before the agent starts,
    so a run that cannot happen costs no LLM and issues no access on the target.
    """
    runtime, runtime_blocker = _resolve_qa_runtime()
    if runtime_blocker:
        return None, runtime_blocker

    if msg.bot_username:
        access_blocker = await preflight_bot_access(
            bot_username=msg.bot_username,
            telethon_env=runtime.telethon_env,
        )
        if access_blocker:
            return None, access_blocker

    qa_result = await run_qa_centrally(
        target=QATarget(
            server_ip=server_info.server_ip,
            ssh_user=server_info.ssh_user,
            project_name=server_info.project_name,
            deployed_url=msg.deployed_url,
            bot_username=msg.bot_username,
        ),
        fleet_ssh_key=server_info.ssh_key,
        acceptance_criteria=acceptance_criteria,
        runtime=runtime,
    )
    return qa_result, None


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
        return live_work_settled({"status": "skipped", "reason": "already_inflight"})

    try:
        # The criteria travel on the message — the producer resolves them from the
        # repository before it creates this run. They decide how QA runs, so parse
        # them first: criteria that only state GET expectations need nothing but
        # the deployed URL.
        acceptance_criteria = msg.acceptance_criteria
        health_checks = parse_health_only_criteria(acceptance_criteria)

        blocker = await check_deployed_url_reachable(msg.deployed_url)
        if blocker:
            return await _handle_qa_blocked(run_id=run_id, blocker=blocker)

        # A server to SSH into and a bot to talk to are what the agent needs, not
        # what the criteria ask for. Resolve them inside the agent branch only —
        # an HTTP check must not fail over an SSH key it never reads.
        server_info = None
        if health_checks is None:
            project = await api_client.get_project(msg.project_id)
            project_name = project_runtime_slug(project)
            server_info = await _resolve_server_info(msg.application_id, project_name)
            if not server_info:
                error = f"Cannot resolve server for application {msg.application_id}"
                logger.error(
                    "qa_server_resolve_failed",
                    application_id=msg.application_id,
                )
                return await _handle_qa_blocked(
                    run_id=run_id,
                    blocker=QABlocker(
                        category=QABlockerCategory.SERVER_UNAVAILABLE,
                        attempted="resolve QA server connection",
                        sent=f"application_id={msg.application_id}",
                        received=error,
                    ),
                )

            # Fail-fast: if project has tg_bot module, bot_username is required
            if not msg.bot_username:
                modules = (project.config or {}).get("modules", [])
                if "tg_bot" in modules:
                    error = (
                        "Project has tg_bot module but bot_username is missing in QAMessage. "
                        "It is stored on the primary repository when the user's Telegram "
                        "token is validated — check that validation ran for this project."
                    )
                    logger.error("qa_bot_username_missing", story_id=story_id, modules=modules)
                    return await _handle_qa_blocked(
                        run_id=run_id,
                        blocker=QABlocker(
                            category=QABlockerCategory.MISSING_BOT_USERNAME,
                            attempted="resolve Telegram bot identity from QA message",
                            sent="QAMessage.bot_username",
                            received=error,
                        ),
                    )

        # A QA run without durable storage must not start an agent that could
        # leave customer data behind. The runner records what the agent did in
        # its own workspace, and the run is where that record lands.
        if health_checks is None:
            if not run_id:
                return await _handle_qa_blocked(
                    run_id=run_id,
                    blocker=QABlocker(
                        category=QABlockerCategory.UNKNOWN,
                        attempted="persist QA cleanup plan",
                        sent="QAMessage.run_id",
                        received=(
                            "agent QA requires a run_id before it can mutate application state"
                        ),
                    ),
                )
        # Mark run as running before starting the checks. A run that already
        # ended is not restarted: the temporary access sweep fails a QA run whose
        # borrowed identity expired, and starting the checks anyway would drive
        # an agent against a bot that has just stopped answering it.
        if run_id:
            start = await api_client.start_run(run_id)
            if not start.started:
                logger.info(
                    "qa_run_already_terminal",
                    run_id=run_id,
                    run_status=start.run_status.value,
                )
                return live_work_settled({"status": "skipped", "reason": start.run_status.value})

        if health_checks is not None:
            logger.info("qa_health_only_criteria", story_id=story_id, checks=len(health_checks))
            qa_result = await run_health_checks(
                deployed_url=msg.deployed_url,
                checks=health_checks,
            )
        else:
            qa_result, exploratory_blocker = await _run_exploratory_qa(
                msg=msg,
                server_info=server_info,
                acceptance_criteria=acceptance_criteria,
            )
            if exploratory_blocker:
                return await _handle_qa_blocked(run_id=run_id, blocker=exploratory_blocker)

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

        if qa_result.blocker:
            return await _handle_qa_blocked(
                run_id=run_id,
                blocker=qa_result.blocker,
                state_changes=qa_result.state_changes,
            )
        if qa_result.passed:
            return await _handle_qa_pass(
                run_id=run_id,
                deployed_url=msg.deployed_url,
                report=qa_result.report,
                state_changes=qa_result.state_changes,
            )
        else:
            return await _handle_qa_fail(
                run_id=run_id,
                qa_attempt=msg.qa_attempt,
                qa_result=qa_result,
            )

    except Exception as exc:
        logger.exception(
            "qa_job_unexpected_error",
            story_id=story_id,
            run_id=run_id,
        )
        return await _handle_qa_blocked(
            run_id=run_id,
            blocker=QABlocker(
                category=QABlockerCategory.UNKNOWN,
                attempted="process QA job",
                sent=f"QAMessage run_id={run_id}",
                received=f"unexpected error: {exc}",
            ),
        )
    finally:
        # Always release inflight marker
        await redis.redis.delete(inflight_key)


async def _handle_qa_pass(
    *,
    run_id: str,
    deployed_url: str,
    report: str = "",
    state_changes: list[dict] | None = None,
) -> dict:
    """Handle QA pass — store PASSED outcome in run."""
    await _update_run(
        run_id,
        RunStatus.COMPLETED,
        QAOutcome.PASSED,
        deployed_url=deployed_url,
        report=report,
        state_changes=state_changes or [],
    )
    logger.info("qa_passed", run_id=run_id)
    return live_work_settled({"status": "passed"})


async def _handle_qa_blocked(
    *,
    run_id: str,
    blocker: QABlocker,
    state_changes: list[dict] | None = None,
) -> dict:
    """Persist a non-product QA blocker for human review."""
    await _update_run(
        run_id,
        RunStatus.COMPLETED,
        QAOutcome.BLOCKED,
        summary="QA could not verify the product",
        blocker=blocker,
        state_changes=state_changes or [],
    )
    logger.warning("qa_blocked", run_id=run_id, category=blocker.category.value)
    return live_work_settled({"status": "qa_blocked", "blocker": blocker.category.value})


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
            state_changes=qa_result.state_changes,
        )
        return live_work_settled({"status": "qa_exhausted"})

    await _update_run(
        run_id,
        RunStatus.COMPLETED,
        QAOutcome.FAILED,
        summary=qa_result.summary,
        failed_checks=failed_checks,
        qa_attempt=qa_attempt,
        report=qa_result.report,
        state_changes=qa_result.state_changes,
    )

    logger.info(
        "qa_failed",
        run_id=run_id,
        attempt=qa_attempt,
    )
    return live_work_settled({"status": "qa_failed"})


async def _update_run(
    run_id: str,
    status: RunStatus,
    qa_outcome: QAOutcome,
    **extra_result: object,
) -> None:
    """Update run status and result with QA outcome.

    A run this worker is still inside can be ended by something outside it —
    the temporary access it borrowed expiring underneath it, for one. That run
    already carries the reason it ended and the API refuses to have it rewritten,
    so the outcome computed here is dropped rather than replacing a named failure
    with a pass. It is the QA job's answer that is stale, not the run's, and the
    consumer keeps going.
    """
    if not run_id:
        logger.warning("qa_no_run_id_skip_update")
        return
    run_result = QARunResult(qa_outcome=qa_outcome, **extra_result)
    try:
        await api_client.patch(
            f"runs/{run_id}",
            json={
                "status": status.value,
                "result": run_result.model_dump(mode="json"),
            },
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != httpx.codes.CONFLICT:
            raise
        logger.warning(
            "qa_run_already_settled",
            run_id=run_id,
            dropped_outcome=qa_outcome.value,
            detail=error.response.text,
        )


def main():
    """Entry point for running as module.

    Only the queue consumer now. The credential refresh loop that kept Claude
    Code's OAuth token alive on every managed server is gone with the agent it
    served: no target holds LLM credentials any more, so there is nothing out
    there to refresh.
    """
    import asyncio
    import signal

    from ._base import _handle_shutdown

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    asyncio.run(run_queue_worker("qa-worker", QA_QUEUE, process_qa_job, group=QA_GROUP))


if __name__ == "__main__":
    main()
