"""Respond command for agent-to-user communication via Redis."""

from datetime import UTC, datetime
import os

from rich.console import Console
import typer

from orchestrator.permissions import require_permission

console = Console()


def _get_redis():
    """Get Redis client (lazy import to avoid dependency issues)."""
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    return redis.from_url(redis_url, decode_responses=True)


def _get_agent_id() -> str:
    """Get agent ID from environment."""
    agent_id = os.getenv("ORCHESTRATOR_AGENT_ID")
    if not agent_id:
        console.print("[bold red]Error:[/bold red] ORCHESTRATOR_AGENT_ID not set")
        raise typer.Exit(code=1)
    return agent_id


RESPONSE_STREAM = "cli-agent:responses"


@require_permission("respond")
def respond(
    message: str,
    expect_reply: bool = typer.Option(
        False,
        "--expect-reply",
        "-q",
        help="Indicates this is a question expecting user reply",
    ),
):
    """Send message to user via Redis stream.

    Use this to communicate with the user. By default, sends a final answer.
    Use --expect-reply when asking a clarifying question.

    Examples:
        orchestrator respond "Task completed successfully"
        orchestrator respond "Which database should I use?" --expect-reply
    """
    agent_id = _get_agent_id()
    r = _get_redis()

    msg_type = "question" if expect_reply else "answer"
    field_name = "question" if expect_reply else "message"

    r.xadd(
        RESPONSE_STREAM,
        {
            "agent_id": agent_id,
            "type": msg_type,
            field_name: message,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    if expect_reply:
        console.print(f"[yellow]?[/yellow] Question sent to user (agent: {agent_id})")
    else:
        console.print(f"[green]✓[/green] Answer sent (agent: {agent_id})")
