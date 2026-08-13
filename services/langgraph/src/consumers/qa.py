"""QA Worker — consumes from qa:queue and runs post-deploy QA testing.

Pure technical worker: only updates run.status and run.result.
Story lifecycle (TESTING → COMPLETED/FAILED) is managed by the dispatcher's
supervise_testing_stories(), which reads run.result.qa_outcome.

Run standalone: python -m src.consumers.qa
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from shared.contracts.acceptance import parse_health_only_criteria
from shared.contracts.dto.incident import IncidentCreate, IncidentType
from shared.contracts.dto.qa_ssh_grant import QA_SSH_GRANT_KEY, QASshGrant
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory, QAFailedCheck, QARunResult
from shared.contracts.dto.telegram import BotLivenessState
from shared.contracts.queues.qa import QAMessage, QAOutcome, QAServerInfo
from shared.contracts.queues.worker import WorkerOwnership
from shared.notifications import notify_admins_best_effort
from shared.qa_identity import (
    QA_SSH_USER_LABEL,
    QAIdentityRejection,
    qa_identity_rejection,
    qa_run_identity,
)
from shared.queues import QA_GROUP, QA_QUEUE
from shared.redis_client import RedisStreamClient
from shared.telegram_access_probe import TelethonCredentialsError, telethon_env

from ..clients.api import api_client, bot_liveness_path
from ..config.settings import get_settings
from ..runtime_identity import project_runtime_slug
from ._base import run_queue_worker, validate_queued_message
from ._live_work import live_work_settled
from ._qa_grant_sweep import qa_grant_sweep_loop
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
# Blockers that say the platform could not run QA, rather than anything about
# the product. Every one of them raises the same administrator alert, and none
# of them may reach the engineering loop.
QA_INFRASTRUCTURE_BLOCKERS = frozenset(
    {
        QABlockerCategory.QA_EXECUTOR_UNAVAILABLE,
        QABlockerCategory.QA_PROBE_UNAVAILABLE,
    }
)
# The bot-liveness question is asked of the platform API, so a failure to get an
# answer is retried exactly as far as a transient network hiccup deserves.
BOT_LIVENESS_ATTEMPTS = 3
BOT_LIVENESS_RETRY_DELAY = 5
# The longest pause this probe sits through between attempts. Telegram's own
# `retry_after` is honoured up to here; a flood-control window longer than this
# is not waited out — the probe stops and reports the infrastructure outcome
# with the number Telegram gave, which keeps the budget bounded either way.
BOT_LIVENESS_MAX_RETRY_DELAY = 30


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

    # The run's identity comes off the server row, not off `ssh_user`: the
    # administrative account is what the fleet key opens, and a run performed as
    # it would be a run with the platform's own authority over the thing it is
    # testing. An empty value here is a host that lends no identity, and the
    # caller refuses it — it is never quietly replaced with `ssh_user`.
    rejection = qa_identity_rejection(server)
    return QAServerInfo(
        server_ip=server.public_ip,
        ssh_user=server.ssh_user,
        qa_ssh_user="" if rejection else qa_run_identity(server),
        ssh_key=ssh_key,
        project_name=project_name,
        server_handle=app.server_handle,
        allocated_ports=frozenset(allocation["port"] for allocation in app.ports),
        qa_identity_rejection=rejection.value if rejection else "",
    )


class RunGrantJournal:
    """The durable record of one run's SSH grant, kept on the run itself.

    The QA run row is where the deploy already leaves its handoff plan, so it is
    where the grant belongs too: it outlives the process that issued the grant,
    it is what the sweep reads, and it is queryable without a second store.
    Writing a single top-level key is enough — the API merges `run_metadata`, so
    this never disturbs the handoff sitting next to it.
    """

    def __init__(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("a QA grant needs a run to be recorded on")
        self._run_id = run_id

    async def write(self, grant: QASshGrant) -> None:
        await api_client.patch(
            f"runs/{self._run_id}",
            json={"run_metadata": {QA_SSH_GRANT_KEY: grant.model_dump(mode="json")}},
        )
        logger.info(
            "qa_ssh_grant_recorded",
            run_id=self._run_id,
            marker=grant.marker,
            state=grant.state.value,
        )


def _resolve_qa_runtime() -> QARuntimeConfig:
    """Say who performs this run and how they reach this runtime.

    Nothing here can fail, and nothing here reads `QA_LLM_*`. The executor is
    the coding agent assigned to testing — Codex by default, Claude Code when
    `QA_EXECUTOR_AGENT_TYPE=claude` explicitly selects it — and its subscription session is a
    directory on the management host that worker-manager mounts into the
    executor container. The API triplet is an optional fallback and is read only
    after that executor has actually failed, which is why an unset triplet is a
    perfectly ordinary production configuration and blocks nothing.

    A missing Telethon setup is not fatal either — a deployment without a bot
    never needs it — so it is reported by the bot preflight, the only place it
    matters.
    """
    settings = get_settings()
    try:
        credentials = telethon_env()
    except TelethonCredentialsError as exc:
        logger.info("qa_telethon_not_configured", detail=str(exc))
        credentials = None
    return QARuntimeConfig(
        executor_agent_type=settings.qa_executor_agent_type,
        capability_host=settings.qa_capability_host,
        telethon_env=credentials,
    )


async def _alert_admins_qa_infrastructure(*, msg: QAMessage, blocker: QABlocker) -> None:
    """Tell an administrator that QA could not run, naming what was unavailable.

    A log line is not an alert. This is the same admin channel the rest of the
    platform's infrastructure failures use, and it carries the identifiers a
    human needs to act: which story, which project, which run, what was
    attempted and what did not answer. One channel for every category in
    `QA_INFRASTRUCTURE_BLOCKERS` — a missing executor and a probe that could not
    be performed are the same kind of fact about the platform, and neither is a
    statement about the product.
    """
    await notify_admins_best_effort(
        f"QA could not run — {blocker.category.value}.\n"
        f"story: {msg.story_id or '(none)'}\n"
        f"project: {msg.project_id}\n"
        f"run: {msg.run_id or '(none)'}\n"
        f"attempted: {blocker.attempted}\n"
        f"missing: {blocker.sent}\n"
        f"detail: {blocker.received}",
        level="error",
        story_id=msg.story_id,
        project_id=msg.project_id,
        run_id=msg.run_id,
    )


async def _probe_bot_liveness(msg: QAMessage) -> tuple[str, QABlocker | None]:
    """Ask, deterministically, whether the deployed bot is live right now.

    The token belongs to the API and stays there: this asks the API, which owns
    it, and gets back a state. Nothing about this call puts a credential in the
    QA runtime or on the deploy target, which is why the question is asked this
    way rather than by handing QA the token.

    Three answers, three destinations. Live is a fact the executor is told and
    does not re-check. A bot Telegram refuses is a deterministic blocker for a
    human — an engineering worker cannot fix a revoked token, so it must not
    become a fix task. Telegram or the API not answering is retried, and then
    reported as QA infrastructure, which is what raises the admin alert.

    "Telegram did not answer" includes being rate limited by it: the API reports
    that as `TELEGRAM_UNREACHABLE` with the `retry_after` Telegram sent, and this
    loop waits that long instead of its own guess — up to
    `BOT_LIVENESS_MAX_RETRY_DELAY`, past which it stops rather than holding a
    consumer slot open for a window it cannot outlast.

    Returns:
        The established fact for the executor, and the blocker if there is one.
    """
    path = bot_liveness_path(msg.project_id)
    detail = ""
    delay = BOT_LIVENESS_RETRY_DELAY
    for attempt in range(BOT_LIVENESS_ATTEMPTS):
        if attempt:
            await asyncio.sleep(delay)
        try:
            liveness = await api_client.get_bot_liveness(msg.project_id)
        except httpx.HTTPError as exc:
            detail = f"the platform API did not answer GET {path}: {exc}"
            delay = BOT_LIVENESS_RETRY_DELAY
            logger.warning("qa_bot_liveness_api_failed", project_id=msg.project_id, error=str(exc))
            continue
        if liveness.state is BotLivenessState.ALIVE:
            logger.info(
                "qa_bot_liveness_confirmed",
                project_id=msg.project_id,
                bot_username=liveness.bot_username,
            )
            return (
                f"- Telegram bot @{liveness.bot_username} answered getMe just before this run. "
                f"The platform API asked on this run's behalf (GET {path}); the bot token stays "
                "in the API and reaches neither this run nor the target."
            ), None
        if liveness.state is BotLivenessState.TELEGRAM_UNREACHABLE:
            detail = f"GET {path} answered {liveness.state.value}: {liveness.detail}"
            logger.warning(
                "qa_bot_liveness_unreachable",
                project_id=msg.project_id,
                detail=detail,
                retry_after=liveness.retry_after,
            )
            delay = liveness.retry_after if liveness.retry_after else BOT_LIVENESS_RETRY_DELAY
            if delay > BOT_LIVENESS_MAX_RETRY_DELAY:
                detail = (
                    f"{detail}; Telegram asked for {liveness.retry_after}s, longer than the "
                    f"{BOT_LIVENESS_MAX_RETRY_DELAY}s this probe waits between attempts"
                )
                break
            continue
        logger.warning(
            "qa_bot_not_live",
            project_id=msg.project_id,
            state=liveness.state.value,
            detail=liveness.detail,
        )
        return "", QABlocker(
            category=QABlockerCategory.BOT_NOT_LIVE,
            attempted=f"confirm @{msg.bot_username} is live before testing it",
            sent=f"GET {path} — the API holds the token and called getMe with it",
            received=f"{liveness.state.value}: {liveness.detail}",
        )
    return "", QABlocker(
        category=QABlockerCategory.QA_PROBE_UNAVAILABLE,
        attempted=f"confirm @{msg.bot_username} is live before testing it",
        sent=f"GET {path} — the API holds the token and calls getMe with it",
        received=f"no answer after {BOT_LIVENESS_ATTEMPTS} attempt(s): {detail}",
    )


class ServerProvisioningJournal:
    """The provisioning journal, addressed at one server, for one kind of fact.

    "This host has no unprivileged account for a QA run" is discovered in two
    places — on the server row before anything connects, and on the target when
    the account the row promised is not there — and it is one fact either way.
    The existing provisioning-failure journal is the mechanism: it is keyed by
    server handle, it upserts into one active episode rather than one row per QA
    run, and an open entry already means "this host's build is not finished" to
    everything that places work, so a host that cannot be QA'd also stops
    receiving new applications until it is repaired.

    A journal write that fails must not turn a blocked run into a crashed
    consumer, so it is logged and the refusal stands either way.
    """

    def __init__(self, server_info: QAServerInfo) -> None:
        self._server = server_info

    async def missing_identity(self, *, reason: QAIdentityRejection, detail: str) -> None:
        handle = self._server.server_handle
        incident = IncidentCreate(
            server_handle=handle,
            incident_type=IncidentType.PROVISIONING_FAILED,
            details={
                "step": "qa_identity",
                "reason": reason.value,
                "detail": detail,
                "server_handle": handle,
                "server_ip": self._server.server_ip,
                "repair": f"python -m src.provisioner.qa_identity_retrofit {handle}",
            },
        )
        try:
            await api_client.record_provisioning_failure(incident)
        except Exception:
            logger.error("qa_identity_incident_write_failed", server_handle=handle, exc_info=True)


async def _missing_identity_blocker(server_info: QAServerInfo) -> QABlocker | None:
    """Refuse a target that lends no QA identity, and record why it was refused.

    Exploratory QA borrows an account on the target, and the whole point of the
    borrowed identity is that it is weaker than the fleet's. Provisioning creates
    that account; the runtime only writes a key into it. A host that has none is
    a host whose provisioning did not finish the job — so the refusal is written
    to the provisioning journal against that server handle, where an
    administrator already looks, instead of being a warning in this consumer's
    log. `python -m src.provisioner.qa_identity_retrofit <handle>` in
    infra-service is what closes it.

    The run itself is blocked, not failed: this is an infrastructure fact about
    the host, not a defect in the user's project.
    """
    if not server_info.qa_identity_rejection:
        return None
    await ServerProvisioningJournal(server_info).missing_identity(
        reason=QAIdentityRejection(server_info.qa_identity_rejection),
        detail=(
            f"servers.labels.{QA_SSH_USER_LABEL} of {server_info.server_handle} "
            "names no account this platform provisioned"
        ),
    )
    return QABlocker(
        category=QABlockerCategory.SERVER_UNAVAILABLE,
        attempted="borrow the target's unprivileged QA account for this run",
        sent=f"servers.labels.{QA_SSH_USER_LABEL} of {server_info.server_handle}",
        received=(
            f"{server_info.qa_identity_rejection}: this host lends no unprivileged account "
            "for a QA run, and exploratory QA is not performed with the fleet's own access"
        ),
    )


async def _run_exploratory_qa(
    *,
    msg: QAMessage,
    server_info: QAServerInfo,
    acceptance_criteria: str,
) -> tuple[QAResult | None, QABlocker | None]:
    """Run the central QA executor against one deployment.

    Returns either a product verdict or the blocker that stopped QA from
    reaching one. What the platform owes the run before an executor starts is
    settled here — an unprivileged account to borrow on the target, and a
    Telegram account the bot admits — so a run that cannot happen issues no
    access on the target and starts no container. Whether an executor exists is
    not one of those preconditions any more: it is discovered by trying, which
    is the only way a subscription session can be checked honestly.
    """
    missing_identity = await _missing_identity_blocker(server_info)
    if missing_identity:
        logger.warning(
            "qa_target_has_no_unprivileged_identity",
            server_handle=server_info.server_handle,
            server_ip=server_info.server_ip,
            rejection=server_info.qa_identity_rejection,
        )
        return None, missing_identity

    runtime = _resolve_qa_runtime()

    established_facts: list[str] = []
    if msg.bot_username:
        # Liveness first: a bot that is not live cannot admit anyone, and the
        # access probe would blame the wrong thing for the same silence.
        bot_fact, liveness_blocker = await _probe_bot_liveness(msg)
        if liveness_blocker:
            if liveness_blocker.category in QA_INFRASTRUCTURE_BLOCKERS:
                await _alert_admins_qa_infrastructure(msg=msg, blocker=liveness_blocker)
            return None, liveness_blocker
        established_facts.append(bot_fact)

        access_blocker = await preflight_bot_access(
            bot_username=msg.bot_username,
            telethon_env=runtime.telethon_env,
        )
        if access_blocker:
            return None, access_blocker

    qa_result = await run_qa_centrally(
        # The QA run owns its executor: the project under test and the run row
        # this message carries. Both exist before any container does.
        ownership=WorkerOwnership(project_id=msg.project_id, run_id=msg.run_id),
        target=QATarget(
            server_ip=server_info.server_ip,
            ssh_user=server_info.ssh_user,
            qa_ssh_user=server_info.qa_ssh_user,
            server_handle=server_info.server_handle,
            project_name=server_info.project_name,
            deployed_url=msg.deployed_url,
            allocated_ports=server_info.allocated_ports,
            bot_username=msg.bot_username,
        ),
        fleet_ssh_key=server_info.ssh_key,
        acceptance_criteria=acceptance_criteria,
        runtime=runtime,
        grant_journal=RunGrantJournal(msg.run_id),
        # A row can promise an account the target no longer has. The runner
        # meets that halfway through, and it is the same provisioning fact the
        # check above refuses on — so it is written to the same journal, against
        # the same handle, rather than ending as a blocked run nobody looks at.
        provisioning_journal=ServerProvisioningJournal(server_info),
        settings=get_settings(),
        established_facts=established_facts,
    )
    if qa_result.blocker is not None and qa_result.blocker.category in QA_INFRASTRUCTURE_BLOCKERS:
        await _alert_admins_qa_infrastructure(msg=msg, blocker=qa_result.blocker)
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

    Two loops. The queue consumer runs QA. Beside it the grant sweep reconciles
    every SSH grant a QA run may still be holding — including the ones this
    process issued before it was last killed, which is the case the runner's own
    `finally` cannot cover.

    The credential refresh loop that kept Claude Code's OAuth token alive on
    every managed server is gone with the agent it served: no target holds LLM
    credentials any more, so there is nothing out there to refresh.
    """
    import asyncio
    import signal

    from ._base import _handle_shutdown

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    async def _run():
        sweep = asyncio.create_task(qa_grant_sweep_loop(), name="qa_grant_sweep")
        consumer = asyncio.create_task(
            run_queue_worker("qa-worker", QA_QUEUE, process_qa_job, group=QA_GROUP),
            name="qa_consumer",
        )
        done, pending = await asyncio.wait([sweep, consumer], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
