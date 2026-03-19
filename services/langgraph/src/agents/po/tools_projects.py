"""PO tools — project management (create, list, get, secrets, telegram validation)."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus, ServiceModule

from .tools_shared import _get_api, _user_headers

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Available modules (single source of truth: ServiceModule enum)
# ---------------------------------------------------------------------------
AVAILABLE_MODULES = {m.value for m in ServiceModule}

HTTP_OK = 200
TELEGRAM_API_TIMEOUT = 10


@tool
async def create_project(
    name: str,
    modules: str = "backend",
    description: str = "",
    *,
    config: RunnableConfig,
) -> str:
    """Create a new project.

    Args:
        name: Project name (lowercase, starts with letter, only a-z/0-9/hyphens).
        modules: Comma-separated modules: backend, tg_bot, notifications, frontend.
        description: What the project should do.
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]

    invalid = [m for m in modules_list if m not in AVAILABLE_MODULES]
    if invalid:
        available = ", ".join(sorted(AVAILABLE_MODULES))
        return f"Error: invalid modules: {', '.join(invalid)}. Available: {available}"

    if "backend" not in modules_list:
        modules_list.insert(0, "backend")

    project_id = str(uuid.uuid4())
    proj_config = {"modules": modules_list, "description": description, "name": name}

    payload = {
        "id": project_id,
        "name": name,
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
        "name": name,
        "git_url": f"pending://{name}",  # placeholder until scaffolder creates GitHub repo
    }
    try:
        repo_resp = await api.post("/api/repositories/", json=repo_payload, headers=headers)
        repo_resp.raise_for_status()
        logger.info("po_repository_created", project_id=project_id, repo_id=repo_resp.json()["id"])
    except Exception:
        logger.warning("po_repository_create_failed", project_id=project_id, exc_info=True)

    return f"Project created. ID: {project['id']}, Name: {project['name']}"


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
        lines.append(f"- {p['name']} (ID: {p['id']}, status: {p.get('status', 'unknown')})")
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
    """Set a secret for a project (e.g. TELEGRAM_BOT_TOKEN).

    Args:
        project_id: Project ID.
        key: Secret key (e.g. TELEGRAM_BOT_TOKEN).
        value: Secret value.
        hint: Description of what the variable is for (e.g. "Telegram bot token for API access").
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
    resp.raise_for_status()
    return f"Secret '{key}' set for project {project_id}."


@tool
async def validate_telegram_token(project_id: str, token: str, *, config: RunnableConfig) -> str:
    """Validate a Telegram bot token and store it as a project secret.

    Call this INSTEAD of set_project_secret when the user provides a Telegram bot token.
    Validates the token via Telegram's getMe API, extracts the bot username,
    and stores both TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME as secrets.

    Args:
        project_id: Project ID.
        token: Telegram bot token from @BotFather (e.g. "123456:ABC-DEF1234...").
    """
    # 1. Validate via getMe
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=TELEGRAM_API_TIMEOUT,
            )
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
        logger.warning("telegram_token_validation_failed", error=str(e))
        return f"Error: could not reach Telegram API — {e}. Please try again."

    data = resp.json()
    if not data.get("ok") or resp.status_code != HTTP_OK:
        description = data.get("description", "Unknown error")
        logger.info("telegram_token_invalid", status=resp.status_code, description=description)
        return (
            f"Error: token is invalid — Telegram returned: {description}. "
            f"Please check the token and try again."
        )

    bot_username = data.get("result", {}).get("username")
    if not bot_username:
        logger.warning("telegram_token_no_username", data=data)
        return "Error: Telegram returned OK but no bot username. The token may be corrupted."

    # 2. Store both secrets
    api = _get_api()
    headers = _user_headers(config)

    await api.post(
        f"/api/projects/{project_id}/config/secrets",
        json={
            "secrets": {"TELEGRAM_BOT_TOKEN": token},
            "env_hints": {"TELEGRAM_BOT_TOKEN": "Telegram bot token from @BotFather"},
        },
        headers=headers,
    )
    await api.post(
        f"/api/projects/{project_id}/config/secrets",
        json={
            "secrets": {"TELEGRAM_BOT_USERNAME": bot_username},
            "env_hints": {
                "TELEGRAM_BOT_USERNAME": (
                    "Bot username (without @) for building t.me links and smoke tests"
                )
            },
        },
        headers=headers,
    )

    # 3. Store bot_username on the primary repository (plain text, not a secret)
    repos_resp = await api.get(f"/api/repositories/?project_id={project_id}", headers=headers)
    repos = repos_resp.json() if repos_resp.status_code == HTTP_OK else []
    primary_repo = next(
        (r for r in repos if r.get("role") == "primary"),
        repos[0] if repos else None,
    )
    if primary_repo:
        await api.patch(
            f"/api/repositories/{primary_repo['id']}",
            json={"bot_username": bot_username},
            headers=headers,
        )

    logger.info(
        "telegram_token_validated",
        project_id=project_id,
        bot_username=bot_username,
    )
    return f"Token valid! Bot: @{bot_username} (https://t.me/{bot_username}). Secrets stored."
