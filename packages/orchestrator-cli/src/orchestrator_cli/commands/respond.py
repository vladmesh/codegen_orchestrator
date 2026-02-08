"""Respond command for agent-to-user communication via Redis."""

import asyncio
from datetime import UTC, datetime
import os

from rich.console import Console
import typer

from orchestrator_cli.client import get_redis_client
from orchestrator_cli.permissions import require_permission

console = Console()

# Stream for user-facing messages
USER_MESSAGE_STREAM = "cli-agent:user-messages"


def _get_agent_id() -> str:
    """Get agent ID from environment."""
    agent_id = os.getenv("ORCHESTRATOR_AGENT_ID") or os.getenv("WORKER_ID")
    if not agent_id:
        console.print("[bold red]Error:[/bold red] ORCHESTRATOR_AGENT_ID or WORKER_ID not set")
        raise typer.Exit(code=1)
    return agent_id


async def send_response_async(message: str, expect_reply: bool = False) -> None:
    """Send message to user via Redis stream."""
    agent_id = _get_agent_id()
    redis_client = get_redis_client()

    msg_type = "question" if expect_reply else "answer"
    field_name = "question" if expect_reply else "message"

    try:
        await redis_client.xadd(
            USER_MESSAGE_STREAM,
            {
                "agent_id": agent_id,
                "type": msg_type,
                field_name: message,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    finally:
        await redis_client.aclose()


@require_permission("respond")
def respond(
    message: str = typer.Argument(..., help="Message to send to user"),
    expect_reply: bool = typer.Option(
        False,
        "--expect-reply",
        "-q",
        help="Indicates this is a question expecting user reply",
    ),
):
    """Send message to user.

    Use this to communicate results or ask questions.

    Examples:
        orchestrator respond "Task completed successfully"
        orchestrator respond "Which database should I use?" --expect-reply
    """
    try:
        asyncio.run(send_response_async(message, expect_reply))

        agent_id = _get_agent_id()
        if expect_reply:
            console.print(f"[yellow]?[/yellow] Question sent to user (agent: {agent_id})")
        else:
            console.print(f"[green]✓[/green] Response sent (agent: {agent_id})")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None
