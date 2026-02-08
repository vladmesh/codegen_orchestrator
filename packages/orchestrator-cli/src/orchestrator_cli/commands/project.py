import asyncio
import json
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator_cli.client import get_api_client
from orchestrator_cli.permissions import require_permission
from shared.contracts.dto.project import ProjectStatus

app = typer.Typer()
console = Console()


# --- Async implementations ---


async def create_project_async(name: str) -> dict:
    """Create a new project via API.

    Note: Scaffolding is triggered separately via engineering flow,
    not directly from project creation.
    """
    api_client = get_api_client()

    project_id = str(uuid.uuid4())
    payload = {
        "id": project_id,
        "name": name,
        "status": ProjectStatus.DRAFT,
        "config": {},
    }

    try:
        response = await api_client.post("/api/projects/", json=payload)
        response.raise_for_status()
        return response.json()
    finally:
        await api_client.aclose()


async def list_projects_async() -> list[dict]:
    """List all projects."""
    api_client = get_api_client()
    try:
        response = await api_client.get("/api/projects/")
        response.raise_for_status()
        return response.json()
    finally:
        await api_client.aclose()


async def get_project_async(project_id: str) -> dict:
    """Get project by ID."""
    api_client = get_api_client()
    try:
        response = await api_client.get(f"/api/projects/{project_id}")
        response.raise_for_status()
        return response.json()
    finally:
        await api_client.aclose()


async def set_secret_async(project_id: str, key: str, value: str) -> dict:
    """Set a secret for a project."""
    api_client = get_api_client()
    try:
        # Get current project
        response = await api_client.get(f"/api/projects/{project_id}")
        response.raise_for_status()
        project = response.json()

        # Update config.secrets
        config = project.get("config") or {}
        secrets = config.get("secrets") or {}
        secrets[key] = value
        config["secrets"] = secrets

        # PATCH project
        response = await api_client.patch(
            f"/api/projects/{project_id}",
            json={"config": config},
        )
        response.raise_for_status()
        return response.json()
    finally:
        await api_client.aclose()


# --- CLI Commands ---


@app.command()
@require_permission("project")
def create(
    name: str = typer.Option(..., "--name", "-n", help="Project name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a new project."""
    try:
        project_data = asyncio.run(create_project_async(name))

        if json_output:
            typer.echo(json.dumps(project_data, indent=2))
            return

        console.print("[bold green]✓ Project created successfully![/bold green]")
        console.print(f"ID: [cyan]{project_data['id']}[/cyan]")
        console.print(f"Name: [magenta]{project_data['name']}[/magenta]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command("list")
@require_permission("project")
def list_projects(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List all projects."""
    try:
        projects = asyncio.run(list_projects_async())

        if json_output:
            typer.echo(json.dumps(projects, indent=2))
            return

        if not projects:
            console.print("[yellow]No projects found.[/yellow]")
            return

        table = Table(title="Projects")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="magenta")
        table.add_column("Status", style="green")

        for p in projects:
            table.add_row(p["id"], p["name"], p.get("status", "unknown"))

        console.print(table)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command()
@require_permission("project")
def get(
    project_id: str = typer.Argument(..., help="Project ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get project details by ID."""
    try:
        project = asyncio.run(get_project_async(project_id))

        if json_output:
            typer.echo(json.dumps(project, indent=2))
            return

        console.print(f"[bold]Project: {project['name']}[/bold]")
        console.print(f"ID: [cyan]{project['id']}[/cyan]")
        status = project.get("status", "unknown")
        status_color = "green" if status == "deployed" else "yellow"
        console.print(f"Status: [{status_color}]{status}[/{status_color}]")

        if project.get("repository_url"):
            console.print(f"Repo: [blue]{project['repository_url']}[/blue]")

        if project.get("config"):
            console.print(f"Config: {project['config']}")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command("set-secret")
@require_permission("project")
def set_secret(
    project_id: str = typer.Option(..., "--project-id", "-p", help="Project ID"),
    key: str = typer.Option(..., "--key", "-k", help="Secret key (e.g., TELEGRAM_TOKEN)"),
    value: str = typer.Option(..., "--value", "-v", help="Secret value"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Set a secret for a project."""
    try:
        result = asyncio.run(set_secret_async(project_id, key, value))

        if json_output:
            typer.echo(json.dumps(result, indent=2))
            return

        console.print("[bold green]✓ Secret set successfully![/bold green]")
        console.print(f"Project: [cyan]{project_id}[/cyan]")
        console.print(f"Key: [yellow]{key}[/yellow]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None
