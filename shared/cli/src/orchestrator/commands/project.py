import json as json_lib
import uuid

from rich.console import Console
from rich.table import Table
import typer

from orchestrator.client import APIClient
from orchestrator.models.project import ProjectCreate, SecretSet
from orchestrator.permissions import require_permission
from orchestrator.validation import validate
from shared.schemas.tool_registry import ToolGroup, register_tool

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
@register_tool(ToolGroup.PROJECT)
@require_permission("project")
def list(json_output: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List all projects for the current user."""
    try:
        projects = client.get("/api/projects", params={"owner_only": "true"})

        if json_output:
            typer.echo(json_lib.dumps(projects, indent=2))
            return

        table = Table(title="Projects")
        table.add_column("ID", justify="right", style="cyan", no_wrap=True)
        table.add_column("Name", style="magenta")
        table.add_column("Status", style="green")

        for p in projects:
            table.add_row(str(p["id"]), p["name"], p["status"])

        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
@register_tool(ToolGroup.PROJECT)
@require_permission("project")
def get(
    project_id: str,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get details of a specific project."""
    try:
        project = client.get(f"/api/projects/{project_id}")

        if json_output:
            typer.echo(json_lib.dumps(project, indent=2))
            return

        console.print(f"[bold]Project: {project['name']} (#{project['id']})[/bold]")
        status_color = "green" if project["status"] == "deployed" else "yellow"
        console.print(f"Status: [{status_color}]{project['status']}[/{status_color}]")
        if project.get("config"):
            console.print("Config:", project["config"])
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
@register_tool(ToolGroup.PROJECT)
@require_permission("project")
@validate(ProjectCreate)
def create(
    name: str = typer.Option(..., "--name", "-n", help="Project name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a new project.

    The project ID is automatically generated as a UUID.

    Args:
        name: Name of the project
    """
    try:
        # Auto-generate UUID for project ID
        project_id = str(uuid.uuid4())

        payload = {
            "id": project_id,
            "name": name,
            "status": "created",
            "config": {},
        }
        response = client.post("/api/projects/", json=payload)

        if json_output:
            typer.echo(json_lib.dumps(response, indent=2))
            return

        console.print("[bold green]✓ Project created successfully![/bold green]")
        console.print(f"ID: [cyan]{response['id']}[/cyan]")
        console.print(f"Name: [magenta]{response['name']}[/magenta]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
        raise typer.Exit(1) from e


@app.command()
@register_tool(ToolGroup.PROJECT)
@require_permission("project")
@validate(SecretSet)
def set_secret(
    project_id: str = typer.Option(..., "--project-id", "-p", help="Project ID"),
    key: str = typer.Option(
        ..., "--key", "-k", help="Secret key (uppercase, e.g., TELEGRAM_TOKEN)"
    ),
    value: str = typer.Option(..., "--value", "-v", help="Secret value"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Set a secret for a project.

    Secrets are stored in project.config.secrets and synced to GitHub Actions.

    Args:
        project_id: Project ID (UUID)
        key: Secret key (must be uppercase with underscores, e.g., TELEGRAM_TOKEN)
        value: Secret value
    """
    try:
        # Get current project
        project = client.get(f"/api/projects/{project_id}")

        # Update config.secrets
        config = project.get("config", {})
        secrets = config.get("secrets", {})
        secrets[key] = value
        config["secrets"] = secrets

        # PATCH project with updated config
        response = client.patch(
            f"/api/projects/{project_id}",
            json={"config": config},
        )

        if json_output:
            typer.echo(json_lib.dumps(response, indent=2))
            return

        console.print("[bold green]✓ Secret set successfully![/bold green]")
        console.print(f"Project: [cyan]{project_id}[/cyan]")
        console.print(f"Key: [yellow]{key}[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
        raise typer.Exit(1) from e
