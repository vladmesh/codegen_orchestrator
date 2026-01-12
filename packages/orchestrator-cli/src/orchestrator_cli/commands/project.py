import asyncio
import uuid

import typer

from orchestrator_cli.client import get_api_client, get_redis_client
from orchestrator_cli.permissions import require_permission
from shared.contracts.dto.project import ProjectStatus


async def create_project_command(name: str):
    """
    Async implementation of create project
    """
    api_client = get_api_client()
    redis_client = get_redis_client()

    # 1. Create Project DTO
    project_id = str(uuid.uuid4())
    # Note: Using Contract's ProjectCreate if needed, or just dict
    # contracts define ProjectCreate with name, description, modules.

    payload = {
        "id": project_id,
        "name": name,
        "status": ProjectStatus.DRAFT,
        "config": {},
    }

    # 2. Call API (The Write)
    try:
        response = await api_client.post("/api/projects/", json=payload)
        response.raise_for_status()
        project_data = response.json()
    except Exception as e:
        # If API fails, we abort.
        raise e
    finally:
        await api_client.aclose()

    # 3. Publish to Redis (The Notification/Side-effect)
    try:
        # We write to scaffolder:queue as per plan
        # Stream payload must be dict of strings/bytes
        stream_payload = {"project_id": project_id, "action": "create", "name": name}
        await redis_client.xadd(name="scaffolder:queue", fields=stream_payload)
    finally:
        await redis_client.aclose()

    return project_data


# Typer command wrapper
app = typer.Typer()


@app.command()
@require_permission("project")
def create(
    name: str = typer.Option(..., "--name", "-n"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a new project"""
    import json

    from rich.console import Console

    console = Console()

    try:
        project_data = asyncio.run(create_project_command(name))

        if json_output:
            typer.echo(json.dumps(project_data, indent=2))
            return

        console.print("[bold green]✓ Project created successfully![/bold green]")
        console.print(f"ID: [cyan]{project_data['id']}[/cyan]")
        console.print(f"Name: [magenta]{project_data['name']}[/magenta]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
        raise typer.Exit(code=1) from None
