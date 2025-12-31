from rich.console import Console
from rich.table import Table
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def list():
    """List all projects for the current user."""
    try:
        projects = client.get("/api/projects", params={"owner_only": "true"})

        table = Table(title="Projects")
        table.add_column("ID", justify="right", style="cyan", no_wrap=True)
        table.add_column("Name", style="magenta")
        table.add_column("Status", style="green")
        # table.add_column("Last Updated", justify="right") # Not returned by API currently

        for p in projects:
            table.add_row(str(p["id"]), p["name"], p["status"])

        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def get(project_id: int):
    """Get details of a specific project."""
    try:
        project = client.get(f"/api/projects/{project_id}")

        console.print(f"[bold]Project: {project['name']} (#{project['id']})[/bold]")
        status_color = "green" if project["status"] == "deployed" else "yellow"
        console.print(f"Status: [{status_color}]{project['status']}[/{status_color}]")
        # console.print(f"Description: {project['description']}")
        if project.get("config"):
            console.print("Config:", project["config"])
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def create(name: str, id: int):
    """Create a new project.

    Args:
        name: Name of the project
        id: Unique integer ID for the project
    """
    try:
        # API expects id, name, status, config
        payload = {"id": id, "name": name, "status": "created", "config": {}}
        response = client.post("/api/projects/", json=payload)
        console.print("[bold green]Project created successfully![/bold green]")
        console.print(f"ID: {response['id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
