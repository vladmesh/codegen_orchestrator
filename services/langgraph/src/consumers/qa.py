"""QA Worker — consumes from qa:queue and runs post-deploy QA testing.

Pure technical worker: only updates run.status and run.result.
Story lifecycle (TESTING → COMPLETED/FAILED) is managed by the dispatcher's
supervise_testing_stories(), which reads run.result.qa_outcome.

Run standalone: python -m src.consumers.qa
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError
import structlog

from shared.contracts.acceptance import (
    ScheduledBehaviourCriterion,
    parse_health_only_criteria,
    parse_scheduled_behaviours,
)
from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.dto.executor_decision import ExecutorDecision
from shared.contracts.dto.incident import IncidentCreate, IncidentType
from shared.contracts.dto.product_brief import InitialSetting, ProductBriefRead
from shared.contracts.dto.qa_ssh_grant import QA_SSH_GRANT_KEY, QASshGrant
from shared.contracts.dto.qa_verification import QAUnverifiedCheck
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
    QAFailedCheck,
    QAProbeLibraryOffer,
    QAProbeRun,
    QARunResult,
)
from shared.contracts.dto.telegram import BotLivenessState
from shared.contracts.queues.qa import QAMessage, QAOutcome, QAServerInfo
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.crypto import decrypt_dict
from shared.notifications import notify_admins_best_effort
from shared.qa_identity import (
    QA_SSH_USER_LABEL,
    QAIdentityRejection,
    qa_identity_rejection,
    qa_run_identity,
)
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION, qa_target_receipt_rejection
from shared.queues import QA_GROUP, QA_QUEUE
from shared.redis import RedisStreamClient
from shared.telegram_access_probe import TelethonCredentialsError, telethon_env

from ..agents.qa.caller_identity import (
    QA_CALLER_IDENTITY_KEY,
    QACallerIdentity,
    caller_identity_facts,
    caller_identity_record,
    resolve_qa_caller_identity,
)
from ..agents.qa.tools import QAJobsCapability
from ..clients.api import api_client, bot_liveness_path
from ..clients.product_settings import GeneratedServiceSettingsClient
from ..config.settings import get_settings
from ..confirmed_settings import confirmed_product_settings
from ..runtime_identity import project_runtime_slug
from ._base import run_queue_worker, validate_queued_message
from ._live_work import live_work_settled
from ._qa_grant_sweep import qa_grant_sweep_loop
from ._qa_probe_library import prepare_probe_library
from ._qa_redaction import JOBS_FIRE_CAPABILITY, QARunRedaction
from ._qa_runner import (
    QA_EXECUTOR_ATTEMPTS,
    QAExecutorAttempts,
    QAResult,
    QARuntimeConfig,
    check_deployed_url_reachable,
    confirmed_settings_facts,
    preflight_bot_access,
    run_health_checks,
    run_qa_centrally,
    scheduled_behaviour_facts,
)
from ._qa_target import QATarget
from ._qa_telegram_identity import (
    QA_TELEGRAM_IDENTITY_KEY,
    identity_record,
    prove_sandbox_telegram_identity,
)
from ._qa_telegram_lease import (
    Holder,
    HolderKind,
    IdentityBusy,
    IdentityHold,
    IdentityOwnershipLost,
    TelegramIdentityLease,
)

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
        QABlockerCategory.QA_TARGET_PROFILE_STALE,
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
# How long a QA run waits for the shared QA Telegram identity while another
# holder (the synthetic buyer between its own waits, or a QA run of another
# consumer) uses it, before it ends as QA infrastructure rather than overlap.
QA_TELEGRAM_IDENTITY_WAIT_SECONDS = 900
QA_TELEGRAM_IDENTITY_POLL_SECONDS = 5


def _telegram_identity_lease(redis: RedisStreamClient) -> TelegramIdentityLease:
    """The one exclusive hold on the QA account every Telegram use of a run takes."""
    return TelegramIdentityLease(redis.redis, QA_TEST_TELEGRAM_ID)


def _identity_hold(lease: TelegramIdentityLease, msg: QAMessage, purpose: str):
    return lease.hold(
        Holder(HolderKind.NATIVE_QA, msg.run_id or f"application:{msg.application_id}", purpose),
        wait_seconds=QA_TELEGRAM_IDENTITY_WAIT_SECONDS,
        poll_seconds=QA_TELEGRAM_IDENTITY_POLL_SECONDS,
    )


def _identity_blocker(refused: IdentityBusy | IdentityOwnershipLost) -> QABlocker:
    """The run could not hold the QA Telegram identity: infrastructure, never a verdict."""
    return QABlocker(
        category=QABlockerCategory.QA_PROBE_UNAVAILABLE,
        attempted="hold the shared QA Telegram identity for this run",
        sent=f"an exclusive hold of Telegram account {QA_TEST_TELEGRAM_ID}",
        received=str(refused),
    )


async def _report_retained_identity(msg: QAMessage, held: IdentityHold) -> None:
    """A run that could not show its Telegram use ended left the identity held: say so."""
    if held.retained is None:
        return
    await notify_admins_best_effort(
        "The QA Telegram identity stays held after a QA run: "
        f"{held.retained}. Nothing else may use the account until an operator who has "
        f"checked that the named use ended releases token {held.token}.\n"
        f"story: {msg.story_id or '(none)'}\nrun: {msg.run_id or '(none)'}",
        level="error",
        story_id=msg.story_id,
        project_id=msg.project_id,
        run_id=msg.run_id,
    )


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
    # The same receipt admission reads: a bound application's host that was
    # never proved, or proved by an older role, is refused before any access.
    receipt = qa_target_receipt_rejection(server)
    return QAServerInfo(
        server_ip=server.public_ip,
        ssh_user=server.ssh_user,
        qa_ssh_user="" if rejection else qa_run_identity(server),
        ssh_key=ssh_key,
        project_name=project_name,
        server_handle=app.server_handle,
        allocated_ports=frozenset(allocation["port"] for allocation in app.ports),
        qa_identity_rejection=rejection.value if rejection else "",
        qa_target_receipt_rejection=receipt.value if receipt else "",
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


def _resolve_qa_runtime(executor_agent_type: AgentType) -> QARuntimeConfig:
    """Say who performs this run and how they reach this runtime.

    Nothing here can fail, and nothing here selects an executor. The coding
    agent was chosen by the API's paid-run resolver and persisted on the Run;
    its subscription session is a directory on the management host that
    worker-manager mounts into the executor container. The API triplet is an
    optional fallback and is read only after that executor has actually failed,
    which is why an unset triplet is a perfectly ordinary production
    configuration and blocks nothing.

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
        executor_agent_type=executor_agent_type,
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


def _stale_target_profile_blocker(server_info: QAServerInfo) -> QABlocker | None:
    """Refuse a host whose readiness receipt does not prove the current QA target profile.

    Nothing is journalled here: the receipt is reconciliation's record, and the
    repair is reconciliation, not another incident written by a QA run.
    """
    if not server_info.qa_target_receipt_rejection:
        return None
    handle = server_info.server_handle
    return QABlocker(
        category=QABlockerCategory.QA_TARGET_PROFILE_STALE,
        attempted="confirm the target's QA harness is the current profile before issuing access",
        sent=f"servers.qa_target_version of {handle}",
        received=(
            f"{server_info.qa_target_receipt_rejection}: {handle} has no readiness receipt for "
            f"QA target profile {QA_TARGET_PROFILE_VERSION}; reconcile it with "
            f"python -m src.provisioner.qa_identity_retrofit {handle}"
        ),
    )


async def _confirmed_qa_brief(story_id: str | None) -> ProductBriefRead | None:
    """The confirmed brief of this story — its settings and input contract — or nothing.

    Read through the released brief endpoint, exactly as the deploy path reads
    them before writing them into the product. A story with no brief, or a
    brief nobody confirmed supplies neither settings nor an input contract.
    """
    if not story_id:
        return None
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or brief.confirmed_at is None:
        return None
    return brief


async def _before_judgement_blocker(msg: QAMessage) -> QABlocker | None:
    """A product QA cannot judge yet: unreachable, or not holding its confirmed values."""
    if blocker := await check_deployed_url_reachable(msg.deployed_url):
        return blocker
    brief = await _confirmed_qa_brief(msg.story_id)
    if brief is None:
        return None
    return await _settings_readback_blocker(
        msg.deployed_url, await confirmed_product_settings(brief, api_client)
    )


async def _settings_readback_blocker(
    deployed_url: str, settings: list[InitialSetting]
) -> QABlocker | None:
    """The deployed product must hold every confirmed value before QA may judge it.

    The values are the confirmed revision's: the brief's own settings and the answers
    its stored capability plan maps to product keys, the same list the deploy seed
    wrote. Each is read back through the product's ordinary settings API; a missing,
    refused or different value is named, and no product judgement is made over it.
    """
    if not settings:
        return None
    proofs = await GeneratedServiceSettingsClient(deployed_url).read_back(settings)
    failed = [
        f"{setting.key} ({setting.scope.value}): {proof.failure.value}"
        for setting, proof in zip(settings, proofs, strict=True)
        if not proof.written
    ]
    if not failed:
        return None
    logger.warning("qa_confirmed_settings_readback_failed", failures=failed)
    return QABlocker(
        category=QABlockerCategory.UNKNOWN,
        attempted="read the confirmed product settings back from the deployed product",
        sent="POST /settings/get for " + ", ".join(setting.key for setting in settings),
        received="confirmed settings not held by the product: " + "; ".join(failed),
    )


async def _stored_secrets(project_id: str) -> dict:
    """The project's own encrypted secrets, decrypted here, in this process's memory.

    Every deployment capability a QA run uses is read through this one call and
    stays on the management host: it is handed to the run's calls as a value,
    never to the executor, its environment, the `qa` CLI or the trace.
    """
    project = await api_client.get_project(project_id)
    if project is None:
        logger.warning("qa_project_secrets_project_missing", project_id=project_id)
        return {}
    stored = (project.config or {}).get("secrets") or {}
    return decrypt_dict(stored) if stored else {}


async def _run_secrets(project_id: str) -> tuple[dict, QARunRedaction]:
    """The project's stored secrets, and the run's one redaction set built from them.

    Both QA legs start here — the exploratory run and the health-only checks — so
    every capability a run may hold is read once and enters the same
    `QARunRedaction` the moment it is read.
    """
    stored = await _stored_secrets(project_id)
    return stored, QARunRedaction.from_stored(stored)


async def _establish_caller_identity(
    msg: QAMessage, stored: dict, *, telegram_account_id: int | None
) -> tuple[QACallerIdentity | None, QABlocker | None]:
    """Choose and prove the one user this run reads package routes as, and record it.

    The one construction of a run's QA identity, for both legs.
    `telegram_account_id` is the QA Telegram account this run proved and the bot
    admitted — passed only by the exploratory leg of a run testing a bot, after
    its bot preflight, so what the executor creates through the bot is what it
    reads back. Every other run, the health-only leg included (it talks to no
    bot), reads as the platform's QA identity. A deployment without the
    caller-identity core gets no identity and is unchanged; a grant that is not
    proved is the typed `QA_ACCESS_GRANT_FAILED` blocker.
    """
    caller_identity, blocker = await resolve_qa_caller_identity(
        deployed_url=msg.deployed_url,
        secrets=stored,
        telegram_account_id=telegram_account_id,
    )
    if blocker:
        return None, blocker
    if (identity := caller_identity_record(caller_identity)) is not None and msg.run_id:
        await api_client.patch(
            f"runs/{msg.run_id}",
            json={"run_metadata": {QA_CALLER_IDENTITY_KEY: identity}},
        )
    return caller_identity, None


async def _run_mechanical_qa(msg, selected, stored, result, redaction, lease):  # noqa: PLR0913
    """Borrow only the identity/target the scheduler has already granted to this Run.

    The probe's Telegram client runs inside the run's exclusive hold on the QA
    account; a client whose disconnect failed leaves that hold retained.
    `IdentityBusy` and `IdentityOwnershipLost` reach the caller, which settles
    them as QA infrastructure rather than as a verdict.
    """
    from shared.contracts.dto.temporary_access import TemporaryAccessStatus  # noqa: PLC0415

    from .mechanical_telegram import (  # noqa: PLC0415
        ProbeFailure,
        describe_cause,
        failure_cause,
        report,
        run_fixed_probe,
    )

    evidence = {"phase": "grant", "status": "running", "run_id": msg.run_id}
    try:
        if not msg.run_id or not msg.bot_username or not result.passed:
            raise ProbeFailure("grant", "fixed probe needs a healthy bot and persisted QA Run")
        grant = await api_client.get_temporary_access_grant(f"tempaccess-{msg.run_id}")
        if (
            grant.status != TemporaryAccessStatus.GRANTED
            or grant.qa_run_id != msg.run_id
            or grant.project_id != msg.project_id
            or grant.target_application_id != msg.application_id
            or grant.target_base_url != msg.deployed_url
            or grant.channel != "telegram"
            or grant.external_id != str(QA_TEST_TELEGRAM_ID)
            or grant.qa_message != msg
        ):
            raise ProbeFailure(
                "grant", "native grant does not own this exact QA target and identity"
            )
        evidence["grant"] = {
            "id": grant.id,
            "head_sha": grant.head_sha,
            "application_id": grant.target_application_id,
            "granted_at": grant.granted_at.isoformat(),
            "status": grant.status.value,
        }
        identity = QACallerIdentity(
            "telegram", str(QA_TEST_TELEGRAM_ID), stored["USER_IDENTITY_CAPABILITY"]
        )
        probe_runner = run_fixed_probe
        probe_arguments = {}
        if selected[0] == "conversation":
            from .stand_conversation import run_probe  # noqa: PLC0415

            probe_runner = run_probe
            probe_arguments["stored"] = stored
        async with _identity_hold(lease, msg, "mechanical") as held:
            try:
                await probe_runner(
                    mode=selected[0],
                    marker=selected[1],
                    bot_username=msg.bot_username,
                    deployed_url=msg.deployed_url,
                    headers=identity.headers(),
                    evidence=evidence,
                    redaction=redaction,
                    **probe_arguments,
                )
            finally:
                if evidence.get("disconnect") == "failed":
                    held.retain("the mechanical probe's Telegram disconnect failed")
                    await _report_retained_identity(msg, held)
        final_grant = await api_client.get_temporary_access_grant(grant.id)
        if final_grant.status != TemporaryAccessStatus.GRANTED:
            raise ProbeFailure("grant", "native grant ended before probe completion")
        evidence["grant_valid_through"] = datetime.now(UTC).isoformat()
        await api_client.patch(
            f"runs/{msg.run_id}",
            json={
                "run_metadata": {
                    QA_CALLER_IDENTITY_KEY: caller_identity_record(identity),
                    "qa_telegram_identity": {"handed_over": True},
                },
            },
        )
        result.checks.append(
            {
                "name": "mechanical Telegram probe",
                "pass": True,
                "detail": "fixed real-chat conversation completed",
            }
        )
    except (IdentityBusy, IdentityOwnershipLost):
        raise
    except Exception as exc:
        # The probe already named what failed inside it (IdentityNotProven and its
        # reason, say); overwriting that with the wrapper's class loses the cause.
        cause = {key: redaction.text(str(value)) for key, value in failure_cause(exc).items()}
        evidence.update(
            status="failed",
            phase=getattr(exc, "phase", evidence["phase"]),
            failure_type=cause["type"],
            failure_cause=cause,
        )
        result.passed = False
        result.checks.append(
            {
                "name": "mechanical Telegram probe",
                "pass": False,
                "detail": redaction.text(f"{evidence['phase']}: {describe_cause(cause)}"),
            }
        )
    result.report = redaction.text(report(evidence))
    return result


async def _resolve_jobs_capability(
    *,
    project_id: str,
    deployed_url: str,
    behaviours: list[ScheduledBehaviourCriterion],
    ownership: WorkerOwnership,
    stored: dict,
) -> QAJobsCapability | None:
    """Resolve this deployment's job-fire capability, here on the management host.

    The same boundary the Telegram credentials and the settings-write
    capability already sit on: the value is read from the project's own
    encrypted secrets in this process, handed to the run's calls, and put in a
    request header by the client. It is never an argument of the `qa` CLI,
    never in the executor's environment, never in the trace and never in a
    verdict.

    A deployment that holds no such capability — an existing product pinned to
    a template older than the jobs core — offers no fire at all rather than a
    fire that cannot be authenticated. The run is told so, and a check that
    needed one fails visibly instead of being quietly skipped.
    """
    if not behaviours:
        return None
    capability = stored.get(JOBS_FIRE_CAPABILITY)
    if not isinstance(capability, str) or not capability:
        logger.info(
            "qa_jobs_capability_unavailable",
            project_id=project_id,
            behaviours=[one.name for one in behaviours],
        )
        return None
    return QAJobsCapability(
        base_url=deployed_url,
        capability=capability,
        # Identity is (fired_by_product, command_id). The product is the one
        # under test, and the run is this QA attempt — the same row the
        # executor's ownership calls its attempt — so a fire is attributable to
        # exactly this QA run and a retry of it reuses that identity.
        fired_by_product=ownership.project_id,
        fired_by_run=ownership.attempt_id,
        behaviours=tuple(behaviours),
    )


async def _run_exploratory_qa(
    *,
    msg: QAMessage,
    server_info: QAServerInfo,
    acceptance_criteria: str,
    attempts: QAExecutorAttempts,
    lease: TelegramIdentityLease,
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

    stale_profile = _stale_target_profile_blocker(server_info)
    if stale_profile:
        logger.warning(
            "qa_target_profile_not_proved",
            server_handle=server_info.server_handle,
            rejection=server_info.qa_target_receipt_rejection,
        )
        await _alert_admins_qa_infrastructure(msg=msg, blocker=stale_profile)
        return None, stale_profile

    executor_decision = await _load_qa_executor_decision(msg.run_id)
    if executor_decision is None:
        return None, QABlocker(
            category=QABlockerCategory.UNKNOWN,
            attempted="read the QA executor decision snapshot",
            sent="QAMessage.run_id",
            received="agent QA requires a paid Run id",
        )
    if isinstance(executor_decision, QABlocker):
        return None, executor_decision
    runtime = _resolve_qa_runtime(executor_decision.agent_type)
    ownership = WorkerOwnership.for_qa(msg)
    run = {
        "msg": msg,
        "server_info": server_info,
        "acceptance_criteria": acceptance_criteria,
        "attempts": attempts,
        "ownership": ownership,
    }
    if not runtime.telethon_env:
        return await _exploratory_run(runtime=runtime, **run)
    # Everything from the identity proof to the removal of the last executor that
    # may hold the session runs inside one exclusive hold on the QA account.
    try:
        async with _identity_hold(lease, msg, "exploratory") as held:
            try:
                return await _exploratory_run(runtime=replace(runtime, telegram_hold=held), **run)
            finally:
                await _report_retained_identity(msg, held)
    except (IdentityBusy, IdentityOwnershipLost) as refused:
        blocker = _identity_blocker(refused)
        await _alert_admins_qa_infrastructure(msg=msg, blocker=blocker)
        return None, blocker


async def _exploratory_run(  # noqa: PLR0913 — one run's whole context, each part named
    *,
    msg: QAMessage,
    server_info: QAServerInfo,
    acceptance_criteria: str,
    attempts: QAExecutorAttempts,
    ownership: WorkerOwnership,
    runtime: QARuntimeConfig,
) -> tuple[QAResult | None, QABlocker | None]:
    """The exploratory run proper, inside the run's hold on the QA Telegram identity."""
    # Proven for this run, or withdrawn for this run: the sandbox is handed the
    # QA Telegram identity only after this, and a refused session is treated
    # from here on exactly as a runtime without Telethon credentials.
    runtime = await prove_sandbox_telegram_identity(runtime)
    if (identity := identity_record(runtime)) is not None and msg.run_id:
        await api_client.patch(
            f"runs/{msg.run_id}",
            json={"run_metadata": {QA_TELEGRAM_IDENTITY_KEY: identity}},
        )

    # Both of these are read here, on the management host, before any executor
    # exists: the behaviour names come off this run's own criteria and the
    # settings off the confirmed brief, so neither is something an executor
    # could have guessed or inferred from prose.
    behaviours = parse_scheduled_behaviours(acceptance_criteria)
    # The project's secrets are read once. Every capability this run may hold
    # comes from here, and so does the one set the run keeps them out of
    # everything with: each call result before the executor sees it, and
    # everything the run retains.
    stored, redaction = await _run_secrets(msg.project_id)
    jobs = await _resolve_jobs_capability(
        project_id=msg.project_id,
        deployed_url=msg.deployed_url,
        behaviours=behaviours,
        ownership=ownership,
        stored=stored,
    )
    brief = await _confirmed_qa_brief(msg.story_id)
    confirmed_brief = brief.content if brief else None
    confirmed_settings = await confirmed_product_settings(brief, api_client) if brief else []
    established_facts: list[str] = [
        *scheduled_behaviour_facts(behaviours, fireable=jobs is not None),
        *confirmed_settings_facts(confirmed_settings),
    ]
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
            identity_refusal=runtime.telegram_identity_refusal,
        )
        if access_blocker:
            return None, access_blocker

    # The user package routes are read as, proved active before any executor
    # exists. A deployment without the caller-identity core is unchanged.
    caller_identity, identity_blocker = await _establish_caller_identity(
        msg, stored, telegram_account_id=QA_TEST_TELEGRAM_ID if msg.bot_username else None
    )
    if identity_blocker:
        return None, identity_blocker
    established_facts.extend(caller_identity_facts(caller_identity))

    # The seeds are due to a run that tests a Telegram bot, which is exactly
    # the run that carries `bot_username`; the project's entries to every run.
    library = await prepare_probe_library(
        project_id=msg.project_id,
        telegram_bot=bool(msg.bot_username),
        read_entries=api_client.list_qa_probes,
    )
    qa_result = await run_qa_centrally(
        # Who the executor belongs to, derived by the one constructor that
        # derives it: the project under test, the run that asked for the work
        # (the same run the developer workers of this project carry), and this
        # QA run row as the attempt. All of it exists before any container does.
        ownership=ownership,
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
        established_facts=established_facts,
        settings_established=bool(confirmed_settings),
        brief=confirmed_brief,
        jobs=jobs,
        attempts=attempts,
        probe_library=library.files,
        caller_identity=caller_identity,
        redaction=redaction,
    )
    qa_result.probe_library = library.offer
    if qa_result.blocker is not None and qa_result.blocker.category in QA_INFRASTRUCTURE_BLOCKERS:
        await _alert_admins_qa_infrastructure(msg=msg, blocker=qa_result.blocker)
    return qa_result, None


async def _load_qa_executor_decision(run_id: str) -> ExecutorDecision | QABlocker | None:
    """Return the immutable QA executor snapshot, or a durable blocker."""
    if not run_id:
        return None
    run = await api_client.get_run(run_id)
    try:
        decision = ExecutorDecision.from_run_metadata(run.run_metadata)
    except (ValueError, ValidationError) as exc:
        return QABlocker(
            category=QABlockerCategory.UNKNOWN,
            attempted="read the QA executor decision snapshot",
            sent="Run.run_metadata.executor_decision",
            received=f"invalid snapshot: {exc}",
        )
    if decision.attempt_kind is not RunType.QA:
        return QABlocker(
            category=QABlockerCategory.UNKNOWN,
            attempted="read the QA executor decision snapshot",
            sent="Run.run_metadata.executor_decision",
            received="snapshot is not for a QA attempt",
        )
    return decision


def _qa_criteria(criteria):
    from .mechanical_telegram import selection  # noqa: PLC0415
    from .stand_conversation import selection as conversation_selection  # noqa: PLC0415

    mechanical = conversation_selection(criteria) or selection(criteria)
    checks = parse_health_only_criteria(mechanical[2] if mechanical else criteria)
    if mechanical and checks is None:
        raise ValueError("fixed stand probes require deterministic HTTP criteria")
    return mechanical, checks


async def _health_caller_identity(msg, stored, mechanical):
    if mechanical:
        return None, None
    return await _establish_caller_identity(msg, stored, telegram_account_id=None)


async def _run_deterministic_qa(msg, checks, mechanical, lease):
    stored, redaction = await _run_secrets(msg.project_id)
    identity, blocker = await _health_caller_identity(msg, stored, mechanical)
    if blocker:
        return None, blocker
    result = await run_health_checks(
        deployed_url=msg.deployed_url, checks=checks, caller_identity=identity, redaction=redaction
    )
    if mechanical:
        try:
            result = await _run_mechanical_qa(msg, mechanical, stored, result, redaction, lease)
        except (IdentityBusy, IdentityOwnershipLost) as refused:
            blocker = _identity_blocker(refused)
            await _alert_admins_qa_infrastructure(msg=msg, blocker=blocker)
            return None, blocker
    return result, None


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

    # The last result QA produced, held for the terminal writers that do not
    # receive it. The executor's transcript lives in this object and nowhere
    # else once its container is deleted, so a settling path that has forgotten
    # the result would settle the Run without evidence this consumer was holding.
    qa_result: QAResult | None = None
    attempts = QAExecutorAttempts(QA_EXECUTOR_ATTEMPTS)

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
        mechanical, health_checks = _qa_criteria(acceptance_criteria)

        blocker = await _before_judgement_blocker(msg)
        if blocker:
            return await _handle_qa_blocked(run_id=run_id, blocker=blocker, attempts=attempts)

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
                    attempts=attempts,
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
                        attempts=attempts,
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
                    attempts=attempts,
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
            # The same identity and redaction an exploratory run has: a package
            # route among the checks is read as the verified QA user, and what the
            # checks retain is scrubbed of every capability the run holds.
            qa_result, identity_blocker = await _run_deterministic_qa(
                msg, health_checks, mechanical, _telegram_identity_lease(redis)
            )
            if identity_blocker:
                return await _handle_qa_blocked(
                    run_id=run_id, blocker=identity_blocker, attempts=attempts
                )
        else:
            qa_result, exploratory_blocker = await _run_exploratory_qa(
                msg=msg,
                server_info=server_info,
                acceptance_criteria=acceptance_criteria,
                attempts=attempts,
                lease=_telegram_identity_lease(redis),
            )
            if exploratory_blocker:
                return await _handle_qa_blocked(
                    run_id=run_id, blocker=exploratory_blocker, attempts=attempts
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

        if qa_result.blocker:
            return await _handle_qa_blocked(
                run_id=run_id,
                attempts=attempts,
                blocker=qa_result.blocker,
                state_changes=qa_result.state_changes,
                telegram_probe_evidence=qa_result.telegram_probe_evidence,
                probe_runs=qa_result.probe_runs,
                probe_library=qa_result.probe_library,
                executor_transcript=qa_result.executor_evidence,
                executor_attempt=qa_result.executor_attempt,
            )
        if qa_result.passed:
            return await _handle_qa_pass(
                run_id=run_id,
                project_id=msg.project_id,
                attempts=attempts,
                deployed_url=msg.deployed_url,
                report=qa_result.report,
                state_changes=qa_result.state_changes,
                telegram_probe_evidence=qa_result.telegram_probe_evidence,
                probe_runs=qa_result.probe_runs,
                probe_library=qa_result.probe_library,
                executor_transcript=qa_result.executor_evidence,
                executor_attempt=qa_result.executor_attempt,
                passed_checks=_passed_check_names(qa_result),
                unverified_checks=qa_result.unverified_checks,
            )
        else:
            return await _handle_qa_fail(
                run_id=run_id,
                project_id=msg.project_id,
                attempts=attempts,
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
            attempts=attempts,
            blocker=QABlocker(
                category=QABlockerCategory.UNKNOWN,
                attempted="process QA job",
                sent=f"QAMessage run_id={run_id}",
                received=f"unexpected error: {exc}",
            ),
            # This is where a first terminal PATCH that failed for anything but
            # a 409 arrives, and QA may already have run: the fallback settles
            # the Run, so it settles it with the evidence the run produced.
            executor_transcript=qa_result.executor_evidence if qa_result else None,
            executor_attempt=qa_result.executor_attempt if qa_result else None,
            probe_runs=qa_result.probe_runs if qa_result else None,
            probe_library=qa_result.probe_library if qa_result else None,
        )
    finally:
        # Always release inflight marker
        await redis.redis.delete(inflight_key)


async def _handle_qa_pass(  # noqa: PLR0913 — one settled pass, each part named
    *,
    run_id: str,
    project_id: str,
    attempts: QAExecutorAttempts,
    deployed_url: str,
    report: str = "",
    state_changes: list[dict] | None = None,
    telegram_probe_evidence: list | None = None,
    probe_runs: list[QAProbeRun] | None = None,
    probe_library: QAProbeLibraryOffer | None = None,
    executor_transcript: str | None = None,
    executor_attempt: EngineeringAttemptLedgerInput | None = None,
    passed_checks: list[str] | None = None,
    unverified_checks: list[QAUnverifiedCheck] | None = None,
) -> dict:
    """Handle QA pass — store PASSED outcome in run, then its probes in the library."""
    settled = await _update_run(
        run_id,
        attempts,
        RunStatus.COMPLETED,
        QAOutcome.PASSED,
        deployed_url=deployed_url,
        report=report,
        state_changes=state_changes or [],
        telegram_probe_evidence=telegram_probe_evidence or [],
        probe_runs=probe_runs,
        probe_library=probe_library,
        executor_transcript=executor_transcript,
        executor_attempt=executor_attempt,
        passed_checks=passed_checks or [],
        unverified_checks=unverified_checks or [],
    )
    logger.info("qa_passed", run_id=run_id)
    if settled and unverified_checks:
        await _record_verification_gaps(project_id=project_id, run_id=run_id)
    if settled and any(probe.exit_status == 0 for probe in probe_runs or []):
        await _store_passed_probes(project_id=project_id, run_id=run_id)
    return live_work_settled({"status": "passed"})


def _passed_check_names(qa_result: QAResult) -> list[str]:
    """The checks this run performed and passed, by name."""
    return [check["name"] for check in qa_result.checks if check.get("pass") is True]


async def _record_verification_gaps(*, project_id: str, run_id: str) -> None:
    """Write this settled Run's unverified checks on its project.

    The API reads them off the settled Run itself. The verdict and the Run are
    already written: a failure here is logged and changes neither.
    """
    try:
        recorded = await api_client.record_verification_gaps_from_run(project_id, run_id)
    except Exception as exc:
        logger.warning(
            "qa_verification_gaps_write_failed",
            project_id=project_id,
            run_id=run_id,
            error=str(exc),
        )
        return
    logger.info(
        "qa_verification_gaps_recorded",
        project_id=project_id,
        run_id=run_id,
        recorded=recorded.recorded,
        already_recorded=recorded.already_recorded,
    )


async def _store_passed_probes(*, project_id: str, run_id: str) -> None:
    """Offer this passed Run's probes to later runs of its project.

    The API reads the probes off the settled Run itself, so what enters the
    library is the record the capability endpoint scrubbed and bounded. The
    verdict and the Run are already written: a failure here is logged and
    changes neither.
    """
    try:
        stored = await api_client.store_qa_probes_from_run(project_id, run_id)
    except Exception as exc:
        logger.warning(
            "qa_probe_library_write_failed", project_id=project_id, run_id=run_id, error=str(exc)
        )
        return
    logger.info(
        "qa_probe_library_updated",
        project_id=project_id,
        run_id=run_id,
        stored=stored.stored,
        evicted=stored.evicted,
        skipped=stored.skipped,
    )


async def _handle_qa_blocked(
    *,
    run_id: str,
    attempts: QAExecutorAttempts,
    blocker: QABlocker,
    state_changes: list[dict] | None = None,
    telegram_probe_evidence: list | None = None,
    probe_runs: list | None = None,
    probe_library: QAProbeLibraryOffer | None = None,
    executor_transcript: str | None = None,
    executor_attempt: EngineeringAttemptLedgerInput | None = None,
) -> dict:
    """Persist a non-product QA blocker for human review."""
    await _update_run(
        run_id,
        attempts,
        RunStatus.COMPLETED,
        QAOutcome.BLOCKED,
        summary="QA could not verify the product",
        blocker=blocker,
        state_changes=state_changes or [],
        telegram_probe_evidence=telegram_probe_evidence or [],
        probe_runs=probe_runs,
        probe_library=probe_library,
        executor_transcript=executor_transcript,
        executor_attempt=executor_attempt,
    )
    logger.warning("qa_blocked", run_id=run_id, category=blocker.category.value)
    return live_work_settled({"status": "qa_blocked", "blocker": blocker.category.value})


async def _handle_qa_fail(
    *,
    run_id: str,
    project_id: str,
    attempts: QAExecutorAttempts,
    qa_attempt: int,
    qa_result: QAResult,
) -> dict:
    """Handle QA fail — store FAILED or EXHAUSTED outcome in run."""
    # An executor's failed check always carries a cause: the runner refuses one
    # without. Checks the runner produced itself (health GETs, package rows) are
    # product verdicts and carry none, so they read as the contract's `product`.
    failed_checks = [
        QAFailedCheck.model_validate(
            {"name": c.get("name", ""), "detail": c.get("detail", "")}
            | ({"cause": c["cause"]} if "cause" in c else {})
        )
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
        settled = await _update_run(
            run_id,
            attempts,
            RunStatus.COMPLETED,
            QAOutcome.EXHAUSTED,
            summary=qa_result.summary,
            failed_checks=failed_checks,
            passed_checks=_passed_check_names(qa_result),
            unverified_checks=qa_result.unverified_checks,
            qa_attempt=qa_attempt,
            report=qa_result.report,
            state_changes=qa_result.state_changes,
            telegram_probe_evidence=qa_result.telegram_probe_evidence,
            probe_runs=qa_result.probe_runs,
            probe_library=qa_result.probe_library,
            executor_transcript=qa_result.executor_evidence,
            executor_attempt=qa_result.executor_attempt,
        )
        if settled and qa_result.unverified_checks:
            await _record_verification_gaps(project_id=project_id, run_id=run_id)
        return live_work_settled({"status": "qa_exhausted"})

    settled = await _update_run(
        run_id,
        attempts,
        RunStatus.COMPLETED,
        QAOutcome.FAILED,
        summary=qa_result.summary,
        failed_checks=failed_checks,
        passed_checks=_passed_check_names(qa_result),
        unverified_checks=qa_result.unverified_checks,
        qa_attempt=qa_attempt,
        report=qa_result.report,
        state_changes=qa_result.state_changes,
        telegram_probe_evidence=qa_result.telegram_probe_evidence,
        probe_runs=qa_result.probe_runs,
        probe_library=qa_result.probe_library,
        executor_transcript=qa_result.executor_evidence,
        executor_attempt=qa_result.executor_attempt,
    )
    if settled and qa_result.unverified_checks:
        await _record_verification_gaps(project_id=project_id, run_id=run_id)

    logger.info(
        "qa_failed",
        run_id=run_id,
        attempt=qa_attempt,
    )
    return live_work_settled({"status": "qa_failed"})


async def _update_run(
    run_id: str,
    attempts: QAExecutorAttempts,
    status: RunStatus,
    qa_outcome: QAOutcome,
    **extra_result: object,
) -> bool:
    """Update run status and result with QA outcome; say whether this write settled it.

    A run this worker is still inside can be ended by something outside it —
    the temporary access it borrowed expiring underneath it, for one. That run
    already carries the reason it ended and the API refuses to have it rewritten,
    so the outcome computed here is dropped rather than replacing a named failure
    with a pass. It is the QA job's answer that is stale, not the run's, and the
    consumer keeps going.
    """
    if not run_id:
        logger.warning("qa_no_run_id_skip_update")
        return False
    extra_result.pop("executor_attempt", None)
    accounting = attempts.accounting
    run_result = QARunResult(qa_outcome=qa_outcome, **extra_result)
    try:
        await api_client.patch(
            f"runs/{run_id}",
            json={
                "status": status.value,
                "result": run_result.model_dump(mode="json"),
                "qa_accounting": accounting.model_dump(mode="json"),
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
        return False
    return True


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
