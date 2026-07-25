"""PO tools — project management (create, list, get, secrets, telegram validation)."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus, ServiceModule
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

HTTP_UNPROCESSABLE = 422


@tool
async def create_project(
    title: str,
    modules: str = "backend",
    description: str = "",
    agent_type: str = AgentType.CLAUDE.value,
    *,
    config: RunnableConfig,
) -> str:
    """Create a new project.

    Args:
        title: Human-readable project title.
        modules: Comma-separated modules: backend, tg_bot, notifications, frontend.
        description: What the project should do.
        agent_type: Developer worker: claude, factory, or codex.
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]

    invalid = [m for m in modules_list if m not in AVAILABLE_MODULES]
    if invalid:
        available = ", ".join(sorted(AVAILABLE_MODULES))
        return f"Error: invalid modules: {', '.join(invalid)}. Available: {available}"

    if agent_type not in AVAILABLE_DEVELOPER_AGENTS:
        available = ", ".join(sorted(AVAILABLE_DEVELOPER_AGENTS))
        return f"Error: invalid agent_type: {agent_type}. Available: {available}"

    if "backend" not in modules_list:
        modules_list.insert(0, "backend")

    project_id = str(uuid.uuid4())
    proj_config = {
        "modules": modules_list,
        "description": description,
        "title": title,
        "agent_type": agent_type,
    }

    payload = {
        "id": project_id,
        "title": title,
        "status": ProjectStatus.DRAFT.value,
        "config": proj_config,
    }

    api = _get_api()
    headers = _user_headers(config)
    resp = await api.post("/api/projects/", json=payload, headers=headers)
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
    repo_resp = await api.post("/api/repositories/", json=repo_payload, headers=headers)
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
    resp = await api.get("/api/projects/", headers=headers)
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
    resp = await api.get(f"/api/projects/{project_id}", headers=headers)
    resp.raise_for_status()
    project = resp.json()
    return json.dumps(project, indent=2, ensure_ascii=False)


@tool
async def set_project_secret(
    project_id: str, key: str, value: str, hint: str = "", *, config: RunnableConfig
) -> str:
    """Set a secret for a project (e.g. OPENROUTER_API_KEY, ADMIN_TELEGRAM_ID).

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

    resp = await api.post(
        f"/api/projects/{project_id}/config/secrets", json=payload, headers=headers
    )
    if resp.status_code == HTTP_UNPROCESSABLE:
        # Bot tokens land here — the server refuses them outside the validator.
        return f"Error: {resp.json()['detail']}"
    resp.raise_for_status()
    return f"Secret '{key}' set for project {project_id}."


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

    resp = await api.post(
        f"/api/projects/{project_id}/telegram/token",
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
        return f"Token rejected ({verdict.reason_code.value}). {verdict.user_message}"
    return f"{verdict.user_message} Token stored for project {project_id}."
