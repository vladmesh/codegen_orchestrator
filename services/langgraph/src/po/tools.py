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

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.crypto import decrypt_dict, encrypt_dict
from shared.queues import (
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
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


# ---------------------------------------------------------------------------
# Available modules (must match copier.yml in service-template)
# ---------------------------------------------------------------------------
AVAILABLE_MODULES = {"backend", "tg_bot", "notifications", "frontend"}


@tool
async def create_project(name: str, modules: str = "backend", description: str = "") -> str:
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

    project_id = str(uuid.uuid4())[:8]
    config = {"modules": modules_list, "description": description, "name": name}

    payload = {
        "id": project_id,
        "name": name,
        "status": ProjectStatus.DRAFT.value,
        "config": config,
    }

    api = _get_api()
    resp = await api.post("/api/projects/", json=payload)
    resp.raise_for_status()
    project = resp.json()
    return f"Project created. ID: {project['id']}, Name: {project['name']}"


@tool
async def list_projects() -> str:
    """List all projects."""
    api = _get_api()
    resp = await api.get("/api/projects/")
    resp.raise_for_status()
    projects = resp.json()

    if not projects:
        return "No projects found."

    lines = []
    for p in projects:
        lines.append(f"- {p['name']} (ID: {p['id']}, status: {p.get('status', 'unknown')})")
    return "\n".join(lines)


@tool
async def get_project(project_id: str) -> str:
    """Get project details by ID.

    Args:
        project_id: Project ID.
    """
    api = _get_api()
    resp = await api.get(f"/api/projects/{project_id}")
    resp.raise_for_status()
    project = resp.json()
    return json.dumps(project, indent=2, ensure_ascii=False)


@tool
async def set_project_secret(project_id: str, key: str, value: str) -> str:
    """Set a secret for a project (e.g. TELEGRAM_BOT_TOKEN).

    Args:
        project_id: Project ID.
        key: Secret key (e.g. TELEGRAM_BOT_TOKEN).
        value: Secret value.
    """
    api = _get_api()

    # Get current project config
    resp = await api.get(f"/api/projects/{project_id}")
    resp.raise_for_status()
    project = resp.json()

    config = project.get("config") or {}
    secrets = config.get("secrets") or {}
    secrets = decrypt_dict(secrets) if secrets else {}
    secrets[key] = value
    config["secrets"] = encrypt_dict(secrets)

    resp = await api.patch(f"/api/projects/{project_id}", json={"config": config})
    resp.raise_for_status()
    return f"Secret '{key}' set for project {project_id}."


@tool
async def trigger_engineering(
    project_id: str,
    action: str = "create",
    description: str | None = None,
    skip_deploy: bool = False,
    *,
    config: RunnableConfig,
) -> str:
    """Trigger engineering task (scaffold + develop + deploy).

    Args:
        project_id: Project ID.
        action: "create" (new project), "feature" (add feature), "fix" (bug fix).
        description: Required for feature/fix — what to build or fix.
        skip_deploy: If true, skip auto-deploy after CI passes.
    """
    if action in ("feature", "fix") and not description:
        return f"Error: --description is required for action '{action}'."

    api = _get_api()

    user_id = config["configurable"].get("user_id", "unknown")
    task_id = f"eng-{uuid.uuid4().hex[:12]}"
    callback_stream = PO_INPUT_QUEUE

    task_data = {
        "id": task_id,
        "type": "engineering",
        "project_id": project_id,
        "task_metadata": {"triggered_by": "po", "action": action},
        "callback_stream": callback_stream,
    }

    resp = await api.post("/api/tasks/", json=task_data)
    resp.raise_for_status()

    eng_msg = EngineeringMessage(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        callback_stream=callback_stream,
        action=action,
        description=description,
        skip_deploy=skip_deploy,
    )
    await _get_stream_client().publish_message(ENGINEERING_QUEUE, eng_msg)

    logger.info("po_engineering_triggered", task_id=task_id, project_id=project_id, action=action)
    return f"Engineering task queued. Task ID: {task_id}"


@tool
async def trigger_deploy(project_id: str, *, config: RunnableConfig) -> str:
    """Trigger deploy-only task (no code changes, just deploy existing code).

    Args:
        project_id: Project ID.
    """
    api = _get_api()

    user_id = config["configurable"].get("user_id", "unknown")
    task_id = f"deploy-{uuid.uuid4().hex[:12]}"
    callback_stream = PO_INPUT_QUEUE

    task_data = {
        "id": task_id,
        "type": "deploy",
        "project_id": project_id,
        "task_metadata": {"triggered_by": "po"},
        "callback_stream": callback_stream,
    }

    resp = await api.post("/api/tasks/", json=task_data)
    resp.raise_for_status()

    deploy_msg = DeployMessage(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        callback_stream=callback_stream,
        triggered_by=DeployTrigger.PO,
    )
    await _get_stream_client().publish_message(DEPLOY_QUEUE, deploy_msg)

    logger.info("po_deploy_triggered", task_id=task_id, project_id=project_id)
    return f"Deploy task queued. Task ID: {task_id}"


@tool
async def get_task_status(task_id: str) -> str:
    """Get task status (engineering or deploy).

    Args:
        task_id: Task ID (e.g. eng-abc123 or deploy-abc123).
    """
    api = _get_api()
    resp = await api.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    task = resp.json()
    return json.dumps(task, indent=2, ensure_ascii=False)


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
    to use more tools. For example: "Starting deployment..." before calling
    trigger_deploy. Your final response is always delivered to the user
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


def get_all_tools() -> list:
    """Return all PO tools for the ReactAgent."""
    return [
        create_project,
        list_projects,
        get_project,
        set_project_secret,
        trigger_engineering,
        trigger_deploy,
        get_task_status,
        set_reminder,
        notify_user,
    ]
