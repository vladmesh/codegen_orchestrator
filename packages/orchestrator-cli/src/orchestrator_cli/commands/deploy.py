"""Deploy commands for orchestrator CLI."""

import asyncio
import json
import os
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator_cli.client import get_api_client, get_redis_client
from orchestrator_cli.permissions import require_permission

app = typer.Typer()
console = Console()


def _get_user_id() -> str:
    """Get user ID from environment."""
    return os.getenv("ORCHESTRATOR_USER_ID", "unknown")


async def trigger_deploy_async(project_id: str) -> dict:
    """Trigger deploy task for a project."""
    api_client = get_api_client()
    redis_client = get_redis_client()

    user_id = _get_user_id()
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

    try:
        response = await api_client.post("/api/tasks/", json=task_data)
        response.raise_for_status()
    finally:
        await api_client.aclose()

    # Publish to deploy queue
    try:
        queue_message = {
            "task_id": task_id,
            "project_id": project_id,
            "user_id": user_id,
            "callback_stream": callback_stream,
        }
        await redis_client.xadd("deploy:queue", {"data": json.dumps(queue_message)})
    finally:
        await redis_client.aclose()

    return {"task_id": task_id, "project_id": project_id, "status": "queued"}


async def get_task_status_async(task_id: str) -> dict:
    """Get task status from API."""
    api_client = get_api_client()
    try:
        response = await api_client.get(f"/api/tasks/{task_id}")
        response.raise_for_status()
        return response.json()
    finally:
        await api_client.aclose()


@app.command()
@require_permission("deploy")
def trigger(
    project_id: str = typer.Option(..., "--project-id", "-p", help="Project ID to deploy"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Trigger deploy task for a project."""
    try:
        result = asyncio.run(trigger_deploy_async(project_id))

        if json_output:
            typer.echo(json.dumps(result, indent=2))
            return

        console.print("[green]✓[/green] Deploy task triggered")
        console.print(f"Task ID: [cyan]{result['task_id']}[/cyan]")
        console.print(f"Project ID: [cyan]{result['project_id']}[/cyan]")
        console.print(
            f"\nMonitor with: [yellow]orchestrator deploy status {result['task_id']}[/yellow]"
        )

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command()
@require_permission("deploy")
def status(
    task_id: str = typer.Argument(..., help="Task ID to check"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Check deploy task status."""
    try:
        task = asyncio.run(get_task_status_async(task_id))

        if json_output:
            typer.echo(json.dumps(task, indent=2))
            return

        table = Table(title=f"Task {task_id}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Type", task.get("type", "unknown"))
        table.add_row("Status", task.get("status", "unknown"))
        table.add_row("Project ID", task.get("project_id") or "N/A")
        table.add_row("Created", task.get("created_at", "N/A"))

        if task.get("started_at"):
            table.add_row("Started", task["started_at"])
        if task.get("completed_at"):
            table.add_row("Completed", task["completed_at"])
        if task.get("error_message"):
            table.add_row("Error", task["error_message"])

        console.print(table)

        if task.get("status") == "completed" and task.get("result"):
            console.print("\n[bold]Result:[/bold]")
            console.print_json(data=task["result"])

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None
