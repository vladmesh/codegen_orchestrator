"""Respond command for agent-to-user communication via Redis.

Writes to po:input so the PO ReactAgent can decide how to forward
the message to the user (via po:proactive stream).
"""

import asyncio
from datetime import UTC, datetime
import os

from rich.console import Console
import typer

from orchestrator_cli.client import get_redis_client
from orchestrator_cli.permissions import require_permission
from shared.queues import PO_INPUT_QUEUE

console = Console()


def _get_agent_id() -> str:
    """Get agent ID from environment."""
    agent_id = os.getenv("ORCHESTRATOR_AGENT_ID") or os.getenv("WORKER_ID")
    if not agent_id:
        console.print("[bold red]Error:[/bold red] ORCHESTRATOR_AGENT_ID or WORKER_ID not set")
        raise typer.Exit(code=1)
    return agent_id


def _get_user_id() -> str:
    """Get user ID from environment."""
    user_id = os.getenv("ORCHESTRATOR_USER_ID", "unknown")
    if user_id == "unknown":
        console.print("[yellow]Warning:[/yellow] ORCHESTRATOR_USER_ID not set, using 'unknown'")
    return user_id


async def send_response_async(message: str) -> None:
    """Send message to user via PO input stream."""
    agent_id = _get_agent_id()
    user_id = _get_user_id()
    redis_client = get_redis_client()

    try:
        await redis_client.xadd(
            PO_INPUT_QUEUE,
            {
                "type": "system_event",
                "event": "agent_message",
                "text": f"[agent:{agent_id}] {message}",
                "user_id": user_id,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    finally:
        await redis_client.aclose()


@require_permission("respond")
def respond(
    message: str = typer.Argument(..., help="Message to send to user"),
):
    """Send message to user.

    Examples:
        orchestrator respond "Task completed successfully"
        orchestrator respond "Which database should I use?"
    """
    try:
        asyncio.run(send_response_async(message))

        agent_id = _get_agent_id()
        console.print(f"[green]✓[/green] Response sent (agent: {agent_id})")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None
