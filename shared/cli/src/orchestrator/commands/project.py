import json as json_lib

from rich.console import Console
from rich.table import Table
import typer

from orchestrator.client import APIClient
from orchestrator.permissions import require_permission

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
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
@require_permission("project")
def get(
    project_id: int,
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
@require_permission("project")
def create(
    name: str,
    id: int,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a new project.

    Args:
        name: Name of the project
        id: Unique integer ID for the project
    """
    try:
        payload = {"id": id, "name": name, "status": "created", "config": {}}
        response = client.post("/api/projects/", json=payload)

        if json_output:
            typer.echo(json_lib.dumps(response, indent=2))
            return

        console.print("[bold green]Project created successfully![/bold green]")
        console.print(f"ID: {response['id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
