import json as json_lib
import os
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator.client import APIClient
from orchestrator.models.engineering import EngineeringTask
from orchestrator.permissions import require_permission
from orchestrator.validation import validate
from shared.schemas.tool_registry import ToolGroup, register_tool

app = typer.Typer()
console = Console()
client = APIClient()


def _get_redis():
    """Get Redis client (lazy import to avoid dependency issues)."""
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    return redis.from_url(redis_url, decode_responses=True)


@app.command()
@register_tool(ToolGroup.ENGINEERING)
@require_permission("engineering")
@validate(EngineeringTask)
def trigger(
    project_id: str = typer.Option(
        ..., "--project-id", "-p", help="Project ID to run engineering task for"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Trigger engineering task for project."""
    try:
        user_id = os.getenv("ORCHESTRATOR_USER_ID", "unknown")
        task_id = f"eng-{uuid.uuid4().hex[:12]}"
        callback_stream = f"agent:events:{user_id}"

        # Create task via API
        task_data = {
            "id": task_id,
            "type": "engineering",
            "project_id": project_id,
            "task_metadata": {"triggered_by": "cli"},
            "callback_stream": callback_stream,
        }

        client.post("/api/tasks/", json=task_data)

        # Publish to engineering queue
        r = _get_redis()
        queue_message = {
            "task_id": task_id,
            "project_id": project_id,
            "user_id": user_id,
            "callback_stream": callback_stream,
        }
        r.xadd("engineering:queue", {"data": json_lib.dumps(queue_message)})

        if json_output:
            typer.echo(
                json_lib.dumps(
                    {"task_id": task_id, "project_id": project_id, "status": "queued"},
                    indent=2,
                )
            )
            return

        console.print("[green]✓[/green] Engineering task triggered")
        console.print(f"Task ID: [cyan]{task_id}[/cyan]")
        console.print(f"Project ID: [cyan]{project_id}[/cyan]")
        console.print(f"\nMonitor with: [yellow]orchestrator engineering status {task_id}[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e


@app.command()
@register_tool(ToolGroup.ENGINEERING)
@require_permission("engineering")
def status(
    task_id: str,
    follow: bool = False,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Check engineering task status."""
    try:
        task = client.get(f"/api/tasks/{task_id}")

        if json_output:
            typer.echo(json_lib.dumps(task, indent=2))
            return

        table = Table(title=f"Task {task_id}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Type", task["type"])
        table.add_row("Status", task["status"])
        table.add_row("Project ID", task.get("project_id") or "N/A")
        table.add_row("Created", task["created_at"])

        if task.get("started_at"):
            table.add_row("Started", task["started_at"])
        if task.get("completed_at"):
            table.add_row("Completed", task["completed_at"])
        if task.get("error_message"):
            table.add_row("Error", task["error_message"])

        console.print(table)

        # Show result if completed
        if task["status"] == "completed" and task.get("result"):
            console.print("\n[bold]Result:[/bold]")
            console.print_json(data=task["result"])

        # Enable follow mode to stream events from Redis
        if follow:
            _stream_events(task_id)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e


def _stream_events(task_id: str):
    """Stream events for a task from Redis."""
    r = _get_redis()
    user_id = os.getenv("ORCHESTRATOR_USER_ID", "unknown")
    stream = f"agent:events:{user_id}"

    # "0" means from beginning of time (see backlog recommendation)
    last_id = "0"

    console.print(f"\n[yellow]⏳ Watching for events in stream: {stream}[/yellow]")

    try:
        while True:
            # Block for 1 second waiting for new events
            events = r.xread({stream: last_id}, block=1000, count=10)

            for _, entries in events:
                for entry_id, data in entries:
                    last_id = entry_id

                    if _process_event(data, task_id):
                        return

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped following.[/yellow]")


def _process_event(data: dict, task_id: str) -> bool:
    """Process a single event. Returns True if following should stop."""
    # Parse event data
    raw_data = data.get("data")
    if not raw_data:
        return False

    try:
        event = json_lib.loads(raw_data)
    except json_lib.JSONDecodeError:
        return False

    # Filter by task_id
    if event.get("task_id") != task_id:
        return False

    # Display event
    event_type = event.get("type", "unknown")
    content = event.get("content", "")

    if event_type == "started":
        console.print("[green]▶ Task started[/green]")
    elif event_type == "completed":
        console.print("[bold green]✓ Task completed successfully[/bold green]")
        if event.get("result"):
            console.print_json(data=event["result"])
        return True
    elif event_type == "failed":
        error_msg = event.get("error", "Unknown error")
        console.print(f"[bold red]✗ Task failed:[/bold red] {error_msg}")
        return True
    elif event_type == "progress":
        console.print(f"[cyan]ℹ[/cyan] {content}")
    else:
        console.print(f"[dim]{event_type}: {content}[/dim]")

    return False
