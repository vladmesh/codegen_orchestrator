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
async def grant_project_user(project_id: str, telegram_id: int, *, config: RunnableConfig) -> str:
    """Request permanent access for one verified Telegram user.

    The deploy worker grants and reads the generated service back after its
    target is healthy. A queued response is not a claim that access is live.
    """
    response = await _get_api().post_raw(
        f"projects/{project_id}/users/grant",
        json={"telegram_id": telegram_id},
        headers=_user_headers(config),
    )
    if response.status_code in {HTTP_FORBIDDEN, HTTP_NOT_FOUND, HTTP_UNPROCESSABLE, HTTP_CONFLICT}:
        return f"Error: {response.json()['detail']}"
    response.raise_for_status()
    body = response.json()
    return (
        f"Permanent access intent {body['intent_id']} is {body['status']}. "
        "It will be live only after the deployed service confirms the user is active."
    )


@tool
async def transfer_project_ownership(
    project_id: str, telegram_id: int, *, config: RunnableConfig
) -> str:
    """Transfer a project only after the incoming owner passes active readback."""
    response = await _get_api().post_raw(
        f"projects/{project_id}/ownership-transfer",
        json={"telegram_id": telegram_id},
        headers=_user_headers(config),
    )
    if response.status_code in {HTTP_FORBIDDEN, HTTP_NOT_FOUND, HTTP_UNPROCESSABLE, HTTP_CONFLICT}:
        return f"Error: {response.json()['detail']}"
    response.raise_for_status()
    body = response.json()
    return (
        f"Ownership-transfer intent {body['intent_id']} is {body['status']}. "
        "Ownership remains unchanged until the service confirms the incoming user is active."
    )


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
