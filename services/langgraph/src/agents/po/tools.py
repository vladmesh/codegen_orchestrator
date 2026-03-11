"""PO ReactAgent tools.

Async tools for the Product Owner agent. Uses shared httpx/redis clients
initialized at consumer startup via init_po_clients().
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import time
from typing import TYPE_CHECKING
import uuid

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus, ServiceModule
from shared.contracts.dto.story import StoryType
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.queues import (
    ARCHITECT_QUEUE,
    PO_PROACTIVE_QUEUE,
    PO_REMINDERS_KEY,
)
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)

# Module-level clients — set by init_po_clients()
_api_client: httpx.AsyncClient | None = None
_stream_client: RedisStreamClient | None = None


def init_po_clients(api_client: httpx.AsyncClient, stream_client: RedisStreamClient) -> None:
    """Initialize shared clients for PO tools. Called once at consumer startup."""
    global _api_client, _stream_client
    _api_client = api_client
    _stream_client = stream_client


def _get_api() -> httpx.AsyncClient:
    if _api_client is None:
        raise RuntimeError("PO tools not initialized — call init_po_clients() first")
    return _api_client


def _get_stream_client() -> RedisStreamClient:
    if _stream_client is None:
        raise RuntimeError("PO tools not initialized — call init_po_clients() first")
    return _stream_client


def _user_headers(config: RunnableConfig) -> dict[str, str]:
    """Extract X-Telegram-ID header from LangGraph config."""
    user_id = config["configurable"].get("user_id", "")
    if user_id:
        return {"X-Telegram-ID": str(user_id)}
    return {}


# ---------------------------------------------------------------------------
# Available modules (single source of truth: ServiceModule enum)
# ---------------------------------------------------------------------------
AVAILABLE_MODULES = {m.value for m in ServiceModule}


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
async def create_story(
    project_id: str,
    title: str,
    description: str,
    story_type: str = "feature",
    *,
    config: RunnableConfig,
) -> str:
    """Create a user story for a project and send it to the architect for decomposition.

    This is the main way to request work on a project — whether creating it
    from scratch, adding features, or fixing bugs. The architect will decompose
    the story into tasks and start engineering work automatically.

    IMPORTANT: The description should contain the full gathered requirements —
    not just the user's original short message. Compose a detailed spec from
    the clarifying conversation before calling this tool.

    Args:
        project_id: Project ID.
        title: Short title for the story (e.g. "Currency rate alerts",
            "Fix login button", "Create telegram bot for recipes").
        description: Detailed description of what to build or fix.
            Include all requirements gathered from the conversation.
        story_type: "feature" (new functionality or project creation),
            "fix" (bug fix).
    """
    api = _get_api()
    headers = _user_headers(config)

    # Determine action from project status, not story_type
    if story_type == "fix":
        action = "fix"
    else:
        proj_resp = await api.get(f"/api/projects/{project_id}", headers=headers)
        proj_resp.raise_for_status()
        project_status = proj_resp.json().get("status", ProjectStatus.DRAFT)
        action = "create" if project_status == ProjectStatus.DRAFT else "feature"

    # 1. Create story via API (API generates the ID)
    story_payload = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "type": StoryType.PRODUCT.value,
        "created_by": "po",
    }
    resp = await api.post("/api/stories/", json=story_payload, headers=headers)
    resp.raise_for_status()
    story_id = resp.json()["id"]
    logger.info("po_story_created", story_id=story_id, project_id=project_id, title=title)

    # 2. Check if project already has an active story (sequential processing)
    user_id = config["configurable"].get("user_id", "unknown")
    active_stories_resp = await api.get(
        f"/api/stories/?project_id={project_id}&status=in_progress", headers=headers
    )
    active_stories = active_stories_resp.json() if active_stories_resp.is_success else []

    if active_stories:
        # Queue the story — it will be triggered when current story completes
        logger.info(
            "po_story_queued",
            story_id=story_id,
            project_id=project_id,
            active_story=active_stories[0]["id"],
        )
        return (
            f"Story created and queued (ID: {story_id}). "
            f"Another story is in progress — this one will start automatically when it completes."
        )

    # No active story — publish to architect:queue for decomposition
    arch_msg = ArchitectMessage(
        story_id=story_id,
        project_id=project_id,
        user_id=user_id,
    )
    await _get_stream_client().publish_message(ARCHITECT_QUEUE, arch_msg)

    # 3. Persist description to project config for action=create
    if action == "create" and description:
        try:
            proj_resp = await api.get(f"/api/projects/{project_id}", headers=headers)
            proj_resp.raise_for_status()
            current_config = proj_resp.json().get("config", {})
            current_config["detailed_spec"] = description
            patch_resp = await api.patch(
                f"/api/projects/{project_id}",
                json={"config": current_config},
                headers=headers,
            )
            patch_resp.raise_for_status()
        except Exception:
            logger.warning(
                "failed_to_persist_detailed_spec",
                project_id=project_id,
                exc_info=True,
            )

    logger.info("po_story_submitted_to_architect", story_id=story_id, action=action)
    return (
        f"Story created and sent to architect for decomposition.\n"
        f"Story: {story_id} — {title}\n"
        f"The architect will break it into tasks and start engineering work."
    )


@tool
async def list_stories(project_id: str, *, config: RunnableConfig) -> str:
    """List all stories for a project.

    Args:
        project_id: Project ID.
    """
    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get(f"/api/stories/?project_id={project_id}", headers=headers)
    resp.raise_for_status()
    stories = resp.json()

    if not stories:
        return "No stories found for this project."

    lines = []
    for s in stories:
        lines.append(f"- [{s['status']}] {s['title']} (ID: {s['id']}, type: {s.get('type', '?')})")
    return "\n".join(lines)


@tool
async def get_story(story_id: str, *, config: RunnableConfig) -> str:
    """Get story details including linked tasks and their statuses.

    Args:
        story_id: Story ID (e.g. story-abc12345).
    """
    api = _get_api()
    headers = _user_headers(config)

    # Get story
    resp = await api.get(f"/api/stories/{story_id}", headers=headers)
    resp.raise_for_status()
    story = resp.json()

    # Get tasks linked to this story
    tasks_resp = await api.get(f"/api/tasks/?story_id={story_id}", headers=headers)
    tasks_resp.raise_for_status()
    tasks = tasks_resp.json()

    result = {
        "story": story,
        "tasks": [{"id": t["id"], "status": t["status"], "type": t["type"]} for t in tasks],
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@tool
async def get_run_status(run_id: str, *, config: RunnableConfig) -> str:
    """Get status of an engineering or deploy run.

    Args:
        run_id: Run ID (e.g. eng-abc123 or deploy-abc123).
    """
    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get(f"/api/runs/{run_id}", headers=headers)
    resp.raise_for_status()
    run = resp.json()
    return json.dumps(run, indent=2, ensure_ascii=False)


@tool
async def set_reminder(delay_minutes: int, reason: str, *, config: RunnableConfig) -> str:
    """Set a reminder to wake up after a delay.

    Use this whenever you need to wait and follow up later — after triggering
    a task, when the user asks to be reminded, or any situation where you
    should check back in the future.

    Args:
        delay_minutes: Minutes until reminder fires.
        reason: Why you're setting this reminder (e.g. "check engineering task eng-abc123").
    """
    redis = _get_stream_client().redis
    user_id = config["configurable"].get("user_id", "unknown")
    fire_at = time.time() + delay_minutes * 60

    reminder = json.dumps(
        {
            "type": "reminder",
            "user_id": user_id,
            "text": reason,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    await redis.zadd(PO_REMINDERS_KEY, {reminder: fire_at})

    logger.info("po_reminder_set", user_id=user_id, delay_minutes=delay_minutes)
    return f"Reminder set for {delay_minutes} minutes: {reason}"


@tool
async def notify_user(message: str, *, config: RunnableConfig) -> str:
    """Send an intermediate message to the user and continue working.

    Use this ONLY when you need to send a progress update while continuing
    to use more tools. For example: "Setting up your project..." before calling
    create_story. Your final response is always delivered to the user
    automatically — do NOT use this tool for final replies.

    Args:
        message: Text to send to the user right now.
    """
    client = _get_stream_client()
    user_id = config["configurable"].get("user_id", "unknown")
    msg = POProactiveMessage(text=message, user_id=user_id)
    await client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(msg))

    logger.info("po_notify_user", user_id=user_id, text_length=len(message))
    return "Message sent to user."


HTTP_OK = 200
TELEGRAM_API_TIMEOUT = 10


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

    logger.info(
        "telegram_token_validated",
        project_id=project_id,
        bot_username=bot_username,
    )
    return f"Token valid! Bot: @{bot_username} (https://t.me/{bot_username}). Secrets stored."


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo.

    Use this to find documentation for third-party APIs or services
    when the user's project needs to integrate with an external service.

    Args:
        query: Search query (e.g. "OpenWeatherMap API documentation").
        max_results: Maximum number of results to return (default 5).
    """
    from duckduckgo_search import DDGS

    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:
        logger.warning("web_search_failed", query=query, error=str(exc))
        return f"Search failed: {exc}"

    if not results:
        return f"No results found for: {query}"

    lines = []
    for r in results:
        lines.append(f"**{r['title']}**")
        lines.append(f"{r['body']}")
        lines.append(f"URL: {r['href']}")
        lines.append("")
    return "\n".join(lines).strip()


def get_all_tools() -> list:
    """Return all PO tools for the ReactAgent."""
    return [
        create_project,
        list_projects,
        get_project,
        set_project_secret,
        validate_telegram_token,
        create_story,
        list_stories,
        get_story,
        get_run_status,
        set_reminder,
        notify_user,
        web_search,
    ]
