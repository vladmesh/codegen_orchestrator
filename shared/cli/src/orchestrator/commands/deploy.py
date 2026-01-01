import json as json_lib
import os
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator.client import APIClient
from orchestrator.permissions import require_permission

app = typer.Typer()
console = Console()
client = APIClient()


def _get_redis():
    """Get Redis client (lazy import to avoid dependency issues)."""
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    return redis.from_url(redis_url, decode_responses=True)


@app.command()
@require_permission("deploy")
def trigger(
    project_id: str,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Trigger deploy task for project."""
    try:
        user_id = os.getenv("ORCHESTRATOR_USER_ID", "unknown")
        task_id = f"deploy-{uuid.uuid4().hex[:12]}"
        callback_stream = f"agent:events:{user_id}"

        # Create task via API
        task_data = {
            "id": task_id,
            "type": "deploy",
            "project_id": project_id,
            "task_metadata": {"triggered_by": "cli"},
            "callback_stream": callback_stream,
        }

        client.post("/api/tasks/", json=task_data)

        # Publish to deploy queue
        r = _get_redis()
        queue_message = {
            "task_id": task_id,
            "project_id": project_id,
            "user_id": user_id,
            "callback_stream": callback_stream,
        }
        r.xadd("deploy:queue", {"data": json_lib.dumps(queue_message)})

        if json_output:
            typer.echo(
                json_lib.dumps(
                    {"task_id": task_id, "project_id": project_id, "status": "queued"},
                    indent=2,
                )
            )
            return

        console.print("[green]✓[/green] Deploy task triggered")
        console.print(f"Task ID: [cyan]{task_id}[/cyan]")
        console.print(f"Project ID: [cyan]{project_id}[/cyan]")
        console.print(f"\nMonitor with: [yellow]orchestrator deploy status {task_id}[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e


@app.command()
@require_permission("deploy")
def status(
    task_id: str,
    follow: bool = False,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Check deploy task status."""
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

        # TODO: Implement --follow mode to stream events from Redis
        if follow:
            console.print("\n[yellow]Note: --follow mode not yet implemented[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1) from e
