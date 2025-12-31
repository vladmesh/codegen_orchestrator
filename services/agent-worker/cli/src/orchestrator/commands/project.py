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
        # Mocking API response for now as endpoints might not match exactly 1:1 yet
        # Real implementation would call: response = client.get("/projects")
        # For Phase 2, we implement the structure.

        # Example data structure expected from API
        projects = [
            {"id": 42, "name": "todo-app", "status": "deployed", "updated_at": "2024-01-15 14:30"},
            {
                "id": 43,
                "name": "analytics-svc",
                "status": "developing",
                "updated_at": "2024-01-15 16:45",
            },
        ]

        table = Table(title="Projects")
        table.add_column("ID", justify="right", style="cyan", no_wrap=True)
        table.add_column("Name", style="magenta")
        table.add_column("Status", style="green")
        table.add_column("Last Updated", justify="right")

        for p in projects:
            table.add_row(str(p["id"]), p["name"], p["status"], p["updated_at"])

        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def get(project_id: int):
    """Get details of a specific project."""
    try:
        # project = client.get(f"/projects/{project_id}")
        project = {
            "id": project_id,
            "name": "todo-app",
            "description": "A simple todo app",
            "status": "deployed",
        }  # Mock

        console.print(f"[bold]Project: {project['name']} (#{project['id']})[/bold]")
        status_color = "green" if project["status"] == "deployed" else "yellow"
        console.print(f"Status: [{status_color}]{project['status']}[/{status_color}]")
        console.print(f"Description: {project['description']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def create(name: str, description: str = ""):
    """Create a new project."""
    try:
        # response = client.post("/projects", json={"name": name, "description": description})
        response = {"id": 44, "name": name, "status": "created"}  # Mock
        console.print("[bold green]Project created successfully![/bold green]")
        console.print(f"ID: {response['id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
