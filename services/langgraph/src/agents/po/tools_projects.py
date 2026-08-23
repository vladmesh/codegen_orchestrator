"""PO tools — project management (create, list, get, secrets, telegram validation)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import (
    ProjectStatus,
    ProjectTeardownResult,
    ServiceModule,
    TeardownStatus,
)
from shared.contracts.dto.telegram import (
    TelegramTokenValidateRequest,
    TelegramTokenVerdict,
    TokenVerdictStatus,
)
from shared.contracts.vocab import AgentType

from .tools_shared import _get_api, _user_headers

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Available modules (single source of truth: ServiceModule enum)
# ---------------------------------------------------------------------------
AVAILABLE_MODULES = {m.value for m in ServiceModule}
AVAILABLE_DEVELOPER_AGENTS = {
    AgentType.CLAUDE.value,
    AgentType.FACTORY.value,
    AgentType.CODEX.value,
}

HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_CONFLICT = 409

# Rollout verdicts the audience endpoints report. The tool repeats them verbatim
# so its report can only mirror what the server said, never embellish it.
ROLLOUT_APPLIED = "applied"
ROLLOUT_PENDING = "pending"
ROLLOUT_FAILED = "failed"
ROLLOUT_NOT_DEPLOYED = "not_deployed"

# A teardown is an SSH `docker compose down` on the project's server: seconds when the
# consumer is free, minutes when it is busy. The tool waits rather than promising a
# free token it has no way to deliver, and gives up long before the user does.
TEARDOWN_POLL_INTERVAL_SECONDS = 5.0
TEARDOWN_TIMEOUT_SECONDS = 300.0
MAX_INITIATING_RUN_ID_LENGTH = 64


def _project_creation_identity(config: RunnableConfig) -> tuple[str, str]:
    """Return the pre-registered identity when a harness supplied one.

    Normal PO requests mint both values here. A live harness may register a
    project in its recovery manifest before it asks the real PO tool to create
    it; that identity travels in RunnableConfig, never in the model-visible
    tool arguments.
    """
    configurable = config.get("configurable", {})
    supplied = configurable.get("project_creation_identity")
    if supplied is None:
        return str(uuid.uuid4()), f"po-{uuid.uuid4().hex[:12]}"
    if not isinstance(supplied, dict):
        raise ValueError("project_creation_identity must be an object")

    project_id = supplied.get("project_id")
    initiating_run_id = supplied.get("initiating_run_id")
    if not isinstance(project_id, str):
        raise ValueError("project_creation_identity.project_id must be a UUID")
    try:
        project_id = str(uuid.UUID(project_id))
    except ValueError as exc:
        raise ValueError("project_creation_identity.project_id must be a UUID") from exc
    if not isinstance(initiating_run_id, str) or not 1 <= len(initiating_run_id) <= (
        MAX_INITIATING_RUN_ID_LENGTH
    ):
        raise ValueError("project_creation_identity.initiating_run_id must be 1-64 characters")
    return project_id, initiating_run_id


@tool
async def create_project(
    title: str,
    modules: str = "backend",
    description: str = "",
    agent_type: str | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Create a new project.

    Args:
        title: Human-readable project title.
        modules: Comma-separated modules: backend, tg_bot, notifications, frontend.
        description: What the project should do.
        agent_type: Optional developer-worker override: claude, factory, or codex.
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]

    invalid = [m for m in modules_list if m not in AVAILABLE_MODULES]
    if invalid:
        available = ", ".join(sorted(AVAILABLE_MODULES))
        return f"Error: invalid modules: {', '.join(invalid)}. Available: {available}"

    if agent_type is not None and agent_type not in AVAILABLE_DEVELOPER_AGENTS:
        available = ", ".join(sorted(AVAILABLE_DEVELOPER_AGENTS))
        return f"Error: invalid agent_type: {agent_type}. Available: {available}"

    if "backend" not in modules_list:
        modules_list.insert(0, "backend")

    project_id, initiating_run_id = _project_creation_identity(config)
    proj_config = {
        "modules": modules_list,
        "description": description,
        "title": title,
    }
    if agent_type is not None:
        proj_config["agent_type"] = agent_type

    payload = {
        "id": project_id,
        "title": title,
        "status": ProjectStatus.DRAFT.value,
        "config": proj_config,
        # The run this project's work is being done for. A user request is the
        # run here — the PO agent is the thing that starts it, so it names it,
        # once, at creation. Every worker created for this project later is
        # stamped with this id. (An experiment matrix supplies its combination's
        # run id in the same field instead of minting one.)
        "initiating_run_id": initiating_run_id,
    }

    api = _get_api()
    headers = _user_headers(config)
    resp = await api.post_raw("projects/", json=payload, headers=headers)
    resp.raise_for_status()
    project = resp.json()

    # Create a Repository record so scaffold_trigger can detect this project.
    # Scaffolder will create the actual GitHub repo and update git_url later.
    repo_payload = {
        "project_id": project_id,
        "name": title,
        # Placeholder until scaffolder creates the GitHub repo.
        "git_url": f"pending://{project['slug']}",
    }
    repo_resp = await api.post_raw("repositories/", json=repo_payload, headers=headers)
    repo_resp.raise_for_status()
    logger.info("po_repository_created", project_id=project_id, repo_id=repo_resp.json()["id"])

    return (
        f"Project created. ID: {project['id']}, Title: {project['title']}, Slug: {project['slug']}"
    )


@tool
async def list_projects(*, config: RunnableConfig) -> str:
    """List all projects."""
    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get_raw("projects/", headers=headers)
    resp.raise_for_status()
    projects = resp.json()

    if not projects:
        return "No projects found."

    lines = []
    for p in projects:
        lines.append(f"- {p['title']} (ID: {p['id']}, status: {p.get('status', 'unknown')})")
    return "\n".join(lines)


@tool
async def get_project(project_id: str, *, config: RunnableConfig) -> str:
    """Get project details by ID.

    Args:
        project_id: Project ID.
    """
    import json

    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get_raw(f"projects/{project_id}", headers=headers)
    resp.raise_for_status()
    project = resp.json()
    return json.dumps(project, indent=2, ensure_ascii=False)


@tool
async def set_project_secret(
    project_id: str, key: str, value: str, hint: str = "", *, config: RunnableConfig
) -> str:
    """Set a secret for a project (e.g. OPENROUTER_API_KEY).

    Telegram bot tokens are refused here — use validate_telegram_token for those.
    Telegram bot audiences are also refused here — use set_bot_access so the
    template contract records the selected access mode.

    Args:
        project_id: Project ID.
        key: Secret key (e.g. OPENROUTER_API_KEY).
        value: Secret value.
        hint: Description of what the variable is for (e.g. "OpenRouter key for LLM calls").
            Hints are stored in plaintext and injected into the Developer Worker prompt
            so it knows which env vars to use in the code.
    """
    api = _get_api()
    headers = _user_headers(config)

    if key == "ADMIN_TELEGRAM_ID":
        return "Error: bot access is managed through set_bot_access."

    payload: dict = {"secrets": {key: value}}
    if hint:
        payload["env_hints"] = {key: hint}

    resp = await api.post_raw(
        f"projects/{project_id}/config/secrets", json=payload, headers=headers
    )
    if resp.status_code == HTTP_UNPROCESSABLE:
        # Bot tokens land here — the server refuses them outside the validator.
        return f"Error: {resp.json()['detail']}"
    resp.raise_for_status()
    return f"Secret '{key}' set for project {project_id}."


@tool
async def set_bot_access(
    project_id: str, mode: str, allowed_telegram_ids: str = "", *, config: RunnableConfig
) -> str:
    """Set a Telegram bot's contract audience.

    Use ``only_me`` without allowed_telegram_ids: the current user's Telegram ID is
    used. Use ``public`` without IDs. For ``custom``, pass the
    comma-separated base audience chosen by the user.

    On an already-deployed bot this also rolls the new audience out to the
    running service and waits (bounded): applied means live, anything else is
    reported exactly as the server phrased it.
    """
    telegram_chat_id = str(config["configurable"]["telegram_chat_id"]).strip()
    if mode == "only_me":
        allowed_telegram_ids = telegram_chat_id
    if mode in {"only_me", "custom"} and not allowed_telegram_ids.strip():
        return "Error: a private bot needs at least one Telegram ID in its audience."

    api = _get_api()
    resp = await api.post_raw(
        f"projects/{project_id}/config/bot-access",
        json={"mode": mode, "allowed_telegram_ids": allowed_telegram_ids},
        headers=_user_headers(config),
    )
    if resp.status_code == HTTP_UNPROCESSABLE:
        return f"Error: {resp.json()['detail']}"
    resp.raise_for_status()
    body = resp.json()
    audience = body.get("allowed_telegram_ids", allowed_telegram_ids)
    prefix = f"Bot access set to '{body['mode']}' for project {project_id}."

    rollout = body.get("rollout", ROLLOUT_NOT_DEPLOYED)
    if rollout != ROLLOUT_PENDING:
        return await _finish_mutation_response(
            prefix=prefix,
            audience=audience,
            rollout=rollout,
            detail="",
            deferred=False,
        )

    telegram_chat_id = str(config["configurable"]["telegram_chat_id"])
    rollout, detail, deferred = await _await_rollout(
        api,
        project_id,
        body["rollout_run_id"],
        config=config,
        telegram_chat_id=telegram_chat_id,
        project_ref=project_id,
    )
    return await _finish_mutation_response(
        prefix=prefix,
        audience=audience,
        rollout=rollout,
        detail=detail,
        deferred=deferred,
    )


# How long the tool waits for a config-only rollout to land before it hands the
# outcome over to the proactive channel instead. The Telegram transport deletes
# the `po:response:{request_id}` stream after PO_RESPONSE_TIMEOUT_S = 60, so a
# synchronous answer must be well inside that window — the remaining margin is
# what the graph's other work (tool calls before this one) may have spent.
ROLLOUT_POLL_INTERVAL_SECONDS = 3.0
ROLLOUT_SYNC_WAIT_SECONDS = 40.0


async def _poll_rollout_once(
    api, project_id: str, run_id: str, *, config: RunnableConfig
) -> tuple[str, str]:
    """One status poll. Transient failures return ("pending", reason) rather
    than raising: an API blip must not be read as a rollout verdict.

    A poll whose HTTP call itself fails is retried by `_await_rollout` only
    until the same bounded deadline — a dead API must not stretch the reply
    past the transport window.
    """
    try:
        poll = await api.get_raw(
            f"projects/{project_id}/config/bot-access/rollouts/{run_id}",
            headers=_user_headers(config),
        )
    except Exception as exc:
        logger.warning("rollout_status_poll_failed", run_id=run_id, error=str(exc))
        return ROLLOUT_PENDING, f"status check failed ({exc}); still trying"
    if poll.status_code == HTTP_NOT_FOUND:
        return ROLLOUT_FAILED, "the rollout run disappeared — it was never recorded"
    # A non-404 error status is the API speaking, not the network flaking: it
    # says the request itself is wrong, and retrying would only repeat it.
    poll.raise_for_status()
    body = poll.json()
    return body.get("rollout", ROLLOUT_PENDING), body.get("detail", "")


async def _await_rollout(
    api,
    project_id: str,
    run_id: str,
    *,
    config: RunnableConfig,
    telegram_chat_id: str,
    project_ref: str = "",
) -> tuple[str, str, bool]:
    """Wait inside the transport window; hand a still-pending verdict onward.

    Returns (status, detail, deferred). Deferred means the rollout had not
    finished when the wait ended and its terminal outcome has been scheduled
    for proactive delivery — the user hears the ending either way, just not
    necessarily inside this reply. The scheduler sweep owns that delivery; it
    reads the same durable run, so nothing depends on this process staying up.
    """
    deadline = asyncio.get_running_loop().time() + ROLLOUT_SYNC_WAIT_SECONDS
    while True:
        rollout, detail = await _poll_rollout_once(api, project_id, run_id, config=config)
        if rollout in {ROLLOUT_APPLIED, ROLLOUT_FAILED}:
            return rollout, detail, False
        if asyncio.get_running_loop().time() >= deadline:
            await notify_rollout_pending(
                project_id=project_id,
                run_id=run_id,
                telegram_chat_id=telegram_chat_id,
                project_ref=project_ref,
            )
            return ROLLOUT_PENDING, detail, True
        await asyncio.sleep(ROLLOUT_POLL_INTERVAL_SECONDS)


async def notify_rollout_pending(
    *, project_id: str, run_id: str, telegram_chat_id: str, project_ref: str = ""
) -> None:
    """Record that this rollout's terminal outcome is still owed to the user.

    The durable marker goes on the rollout run itself (idempotent: written
    once and flipped to delivered by whoever reports first), and the scheduler
    sweep turns it into a proactive message when the run reaches applied or
    failed. The sweep reads the same durable run, so nothing depends on this
    process staying up.
    """
    api = _get_api()
    try:
        await api.post_raw(
            f"projects/{project_id}/config/bot-access/rollouts/{run_id}/notify-owed",
            json={},
            headers=_user_headers(_config_with_chat(telegram_chat_id)),
        )
    except Exception as exc:
        # The marker write is best-effort from the tool: the sweep reconciles
        # owed notifications from the run's records even if this call never
        # lands, and the next status poll of this same rollout would owe it
        # again if the conversation were still open.
        logger.warning("rollout_notify_owe_failed", run_id=run_id, error=str(exc))
        return
    logger.info(
        "rollout_terminal_notification_owed",
        run_id=run_id,
        project_id=project_id,
    )


def _config_with_chat(telegram_chat_id: str) -> dict:
    return {"configurable": {"telegram_chat_id": telegram_chat_id}}


def _rollout_report(status: str, detail: str) -> str:
    """The user-facing sentence for one rollout outcome. Truthful by construction:
    only `applied` says anything reached the running bot."""
    if status == "applied":
        return "The change is live on the running bot now."
    if status == "failed":
        return (
            f"The configuration changed, but applying it to the running bot FAILED"
            f"{': ' + detail if detail else ''}. The bot is still running with the "
            "old audience — tell the user, and do not say the access changed."
        )
    return (
        "The configuration is saved, but the rollout has not finished yet "
        "(it is still being applied to the running bot). Tell the user it is "
        "in progress and check again in a few minutes — do not say the access "
        "changed live until it is confirmed."
    )


async def _finish_mutation_response(
    *,
    prefix: str,
    audience: str,
    rollout: str,
    detail: str,
    deferred: bool,
) -> str:
    """Assemble the truthful final text for one mutation outcome."""
    if rollout == ROLLOUT_NOT_DEPLOYED:
        return (
            f"{prefix} Current audience: {audience}. The project is not deployed, "
            "so there is nothing to apply to — the audience takes effect at the "
            "next deploy."
        )
    if rollout == ROLLOUT_APPLIED:
        return f"{prefix} Current audience: {audience}. {_rollout_report(rollout, detail)}"
    report = _rollout_report(rollout, detail)
    if deferred:
        report += " I will message you here as soon as the rollout finishes, whichever way it ends."
    return f"{prefix} Current audience: {audience}. {report}"


async def _mutate_bot_user(
    project_id: str,
    telegram_id: int,
    *,
    method: str,
    config: RunnableConfig,
) -> str:
    """One typed audience mutation plus a truthful rollout report.

    The server owns the audience arithmetic; this tool sends exactly one ID and
    never reconstructs the comma-separated list.
    """
    api = _get_api()
    headers = _user_headers(config)
    if method == "POST":
        resp = await api.post_raw(
            f"projects/{project_id}/config/bot-access/users",
            json={"telegram_id": telegram_id},
            headers=headers,
        )
    else:
        resp = await api.delete_raw(
            f"projects/{project_id}/config/bot-access/users/{telegram_id}",
            headers=headers,
        )
    if resp.status_code in {HTTP_FORBIDDEN, HTTP_NOT_FOUND, HTTP_UNPROCESSABLE, HTTP_CONFLICT}:
        return f"Error: {resp.json()['detail']}"
    resp.raise_for_status()
    body = resp.json()

    operation = body["operation"]
    audience = body["audience"]
    if operation in {"already_present", "already_absent"}:
        return (
            f"Telegram ID {telegram_id} was already "
            f"{'in' if operation == 'already_present' else 'not in'} the audience — "
            f"nothing changed. Current audience: {audience}."
        )

    verb = "added to" if operation == "added" else "removed from"
    prefix = f"Telegram ID {telegram_id} {verb} the audience."
    rollout = body["rollout"]

    if rollout != ROLLOUT_PENDING:
        return await _finish_mutation_response(
            prefix=prefix,
            audience=audience,
            rollout=rollout,
            detail="",
            deferred=False,
        )

    telegram_chat_id = str(config["configurable"]["telegram_chat_id"])
    rollout, detail, deferred = await _await_rollout(
        api,
        project_id,
        body["rollout_run_id"],
        config=config,
        telegram_chat_id=telegram_chat_id,
        project_ref=project_id,
    )
    return await _finish_mutation_response(
        prefix=prefix,
        audience=audience,
        rollout=rollout,
        detail=detail,
        deferred=deferred,
    )


@tool
async def add_bot_user(project_id: str, telegram_id: int, *, config: RunnableConfig) -> str:
    """Add ONE Telegram user ID to a bot's allowed audience.

    Use this when the user says "add user ID X to my bot". The ID is added to the
    existing audience — nobody else loses access. For an already-deployed bot the
    change is rolled out to the running bot automatically; the tool answers only
    after the rollout is confirmed (or reports honestly that it is still pending).

    Args:
        project_id: Project ID.
        telegram_id: The Telegram user ID to allow.
    """
    return await _mutate_bot_user(project_id, telegram_id, method="POST", config=config)


@tool
async def remove_bot_user(project_id: str, telegram_id: int, *, config: RunnableConfig) -> str:
    """Remove ONE Telegram user ID from a bot's allowed audience.

    Use this when the user says "remove user ID X from my bot". Only that ID is
    removed; the rest of the audience is preserved. The owner stays in the
    audience unless they are removed explicitly. Removing the final allowed ID is
    refused — making the bot public is set_bot_access's explicit decision.

    Args:
        project_id: Project ID.
        telegram_id: The Telegram user ID to revoke.
    """
    return await _mutate_bot_user(project_id, telegram_id, method="DELETE", config=config)


@tool
async def teardown_project(project_id: str, *, config: RunnableConfig) -> str:
    """Tear down one of the user's projects: take it offline and free its Telegram bot.

    Use it when the user asks to remove, shut down or unlink a project, and when they
    want to reuse a bot token their own older project is holding. Only the owner can
    do this. Confirm with the user before calling — the running project goes down.

    The tool waits for the containers to actually stop before reporting the bot free.
    Bind the token to another project ONLY after this says the bot is free: until the
    old bot stops polling, Telegram refuses the second one. If it comes back still
    shutting down, tell the user and call this tool again later.

    Args:
        project_id: Project ID to tear down.
    """
    api = _get_api()
    headers = _user_headers(config)

    resp = await api.post_raw(f"projects/{project_id}/teardown", headers=headers)
    if resp.status_code in (HTTP_FORBIDDEN, HTTP_NOT_FOUND):
        # Someone else's project, or none at all — the user gets told, not a stack trace.
        return f"Error: {resp.json()['detail']}"
    resp.raise_for_status()
    result = ProjectTeardownResult.model_validate(resp.json())

    deadline = asyncio.get_running_loop().time() + TEARDOWN_TIMEOUT_SECONDS
    while result.status == TeardownStatus.PENDING:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(TEARDOWN_POLL_INTERVAL_SECONDS)
        poll = await api.get_raw(f"projects/{project_id}/teardown", headers=headers)
        poll.raise_for_status()
        result = ProjectTeardownResult.model_validate(poll.json())

    logger.info(
        "po_project_teardown_result",
        project_id=project_id,
        status=result.status.value,
        pending=result.pending_application_ids,
        released_bot=result.released_bot_username,
    )

    if result.status == TeardownStatus.FAILED:
        return (
            f"Teardown of project {project_id} failed: {result.error}. "
            "The project is still up and still holds its bot — do not reuse the token."
        )
    if result.status == TeardownStatus.PENDING:
        return (
            f"Project {project_id} is still shutting down "
            f"({len(result.pending_application_ids)} application(s) left). "
            "Its bot is still running, so the token cannot be used elsewhere yet. "
            "Tell the user it takes a few more minutes and call teardown_project again later."
        )

    bot = (
        f"Bot @{result.released_bot_username} is free — its token can now be bound "
        "to another project."
        if result.released_bot_username
        else "It holds no bot any more, so its token is free to use elsewhere."
    )
    return f"Project {project_id} is down and archived. {bot}"


@tool
async def validate_telegram_token(project_id: str, token: str, *, config: RunnableConfig) -> str:
    """Validate a Telegram bot token and bind it to the project.

    The ONLY way to attach a Telegram bot token — set_project_secret rejects it.
    The server validates the token, stores it and the bot username, and returns the
    verdict. Relay the message back to the user; if the token was rejected, ask for
    a new one.

    Args:
        project_id: Project ID.
        token: Telegram bot token from @BotFather (e.g. "123456:ABC-DEF1234...").
    """
    api = _get_api()
    headers = _user_headers(config)

    resp = await api.post_raw(
        f"projects/{project_id}/telegram/token",
        json=TelegramTokenValidateRequest(token=token).model_dump(),
        headers=headers,
    )
    resp.raise_for_status()
    verdict = TelegramTokenVerdict.model_validate(resp.json())

    logger.info(
        "telegram_token_verdict",
        project_id=project_id,
        status=verdict.status.value,
        reason_code=verdict.reason_code,
    )

    if verdict.status == TokenVerdictStatus.REJECTED:
        rejection = f"Token rejected ({verdict.reason_code.value}). {verdict.user_message}"
        if verdict.conflict_project_id is None:
            return rejection
        # The holder is the user's own project, so the deadlock has a way out:
        # keep working there, or tear it down and take the token back.
        return (
            f"{rejection} The bot is held by project {verdict.conflict_project_id}. "
            "Ask the user which they want: continue work in that project, or free the "
            f"token — teardown_project('{verdict.conflict_project_id}') takes it offline "
            "and waits for it to stop. Call validate_telegram_token again with the same "
            "token only once that tool reports the bot free; while it is still shutting "
            "down the old bot holds the token and the retry would fail."
        )
    return f"{verdict.user_message} Token stored for project {project_id}."
