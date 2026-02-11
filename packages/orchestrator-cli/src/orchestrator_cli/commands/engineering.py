"""Engineering commands for orchestrator CLI."""

import asyncio
import json
import os
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator_cli.client import get_api_client, get_redis_client
from orchestrator_cli.permissions import require_permission
from shared.schemas.tool_groups import ToolGroup
from shared.schemas.tool_registry import register_tool

app = typer.Typer()
console = Console()


def _get_user_id() -> str:
    """Get user ID from environment."""
    return os.getenv("ORCHESTRATOR_USER_ID", "unknown")


async def trigger_engineering_async(
    project_id: str,
    action: str = "create",
    description: str | None = None,
    skip_deploy: bool = False,
) -> dict:
    """Trigger engineering task for a project."""
    api_client = get_api_client()
    redis_client = get_redis_client()

    user_id = _get_user_id()
    task_id = f"eng-{uuid.uuid4().hex[:12]}"
    callback_stream = f"agent:events:{user_id}"

    # Create task via API
    task_data = {
        "id": task_id,
        "type": "engineering",
        "project_id": project_id,
        "task_metadata": {"triggered_by": "cli", "action": action},
        "callback_stream": callback_stream,
    }

    try:
        response = await api_client.post("/api/tasks/", json=task_data)
        response.raise_for_status()
    finally:
        await api_client.aclose()

    # Publish to engineering queue
    try:
        queue_message = {
            "task_id": task_id,
            "project_id": project_id,
            "user_id": user_id,
            "callback_stream": callback_stream,
            "action": action,
            "description": description,
            "skip_deploy": skip_deploy,
        }
        await redis_client.xadd("engineering:queue", {"data": json.dumps(queue_message)})
    finally:
        await redis_client.aclose()

    return {"task_id": task_id, "project_id": project_id, "action": action, "status": "queued"}


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
@require_permission("engineering")
def trigger(
    project_id: str = typer.Option(
        ..., "--project-id", "-p", help="Project ID to run engineering task for"
    ),
    action: str = typer.Option(
        "create", "--action", "-a", help="Action type: create, feature, or fix"
    ),
    description: str = typer.Option(
        None, "--description", "-d", help="Task description (required for feature/fix)"
    ),
    skip_deploy: bool = typer.Option(
        False, "--skip-deploy", help="Skip auto-deploy after CI passes"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Trigger engineering task for a project."""
    try:
        result = asyncio.run(
            trigger_engineering_async(project_id, action, description, skip_deploy)
        )

        if json_output:
            typer.echo(json.dumps(result, indent=2))
            return

        console.print("[green]✓[/green] Engineering task triggered")
        console.print(f"Task ID: [cyan]{result['task_id']}[/cyan]")
        console.print(f"Project ID: [cyan]{result['project_id']}[/cyan]")
        console.print(
            f"\nMonitor with: [yellow]orchestrator engineering status {result['task_id']}[/yellow]"
        )

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command()
@require_permission("engineering")
def status(
    task_id: str = typer.Argument(..., help="Task ID to check"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Check engineering task status."""
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


async def update_framework_async(project_id: str) -> dict:
    """Send copier update request to scaffolder queue."""
    api_client = get_api_client()
    redis_client = get_redis_client()

    try:
        # Fetch project to get repo info
        response = await api_client.get(f"/api/projects/{project_id}")
        response.raise_for_status()
        project = response.json()
    finally:
        await api_client.aclose()

    repo_url = project.get("repository_url", "")
    if not repo_url or "github.com/" not in repo_url:
        raise ValueError(
            f"Project {project_id} has no valid GitHub repository URL. "
            "Cannot update framework without an existing repository."
        )

    # Extract org/repo from URL
    repo_full_name = repo_url.split("github.com/")[-1].rstrip("/").removesuffix(".git")
    project_name = project.get("name", project_id)

    # Build scaffolder message
    from shared.contracts.queues.scaffolder import ScaffolderAction, ScaffolderMessage

    msg = ScaffolderMessage(
        request_id=str(uuid.uuid4()),
        action=ScaffolderAction.UPDATE,
        project_id=project_id,
        project_name=project_name,
        repo_full_name=repo_full_name,
    )

    try:
        await redis_client.xadd("scaffolder:queue", {"data": msg.model_dump_json()})
    finally:
        await redis_client.aclose()

    return {
        "project_id": project_id,
        "repo_full_name": repo_full_name,
        "status": "queued",
    }


@app.command()
@register_tool(ToolGroup.ENGINEERING)
def update_framework(
    project_id: str = typer.Option(
        ..., "--project-id", "-p", help="Project ID to update framework for"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Update project framework using copier update."""
    try:
        result = asyncio.run(update_framework_async(project_id))

        if json_output:
            typer.echo(json.dumps(result, indent=2))
            return

        console.print("[green]✓[/green] Framework update queued")
        console.print(f"Project ID: [cyan]{result['project_id']}[/cyan]")
        console.print(f"Repository: [cyan]{result['repo_full_name']}[/cyan]")
        console.print(
            "\n[dim]The scaffolder will clone, run copier update, and push changes.[/dim]"
        )

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None
