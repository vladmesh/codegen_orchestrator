from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def trigger(project_id: int):
    """Trigger a deployment for a project."""
    try:
        # API returns task_id
        response = client.post("/api/deploys/trigger", json={"project_id": project_id})

        console.print("[bold green]Deployment triggered![/bold green]")
        console.print(f"Task ID: {response['task_id']}")
        console.print(f"Monitor status with: orchestrator deploy status {response['task_id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def status(task_id: str):
    """Get status of a deployment task."""
    try:
        # Check endpoint
        response = client.get(f"/api/deploys/tasks/{task_id}")

        console.print(f"Task: {task_id}")
        console.print(f"Status: [yellow]{response['status']}[/yellow]")
        console.print("[dim]Last step:[/dim]")
        console.print(f"  {response.get('last_step', 'Unknown')}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def logs(project_id: int, lines: int = 100):
    """Get deployment logs for a project."""
    try:
        console.print(f"Fetching logs for project {project_id}...")
        # Note: This might return raw text or JSON depending on implementation.
        # Assuming JSON list of log lines or raw text for now.
        response = client.get(f"/api/deploys/logs/{project_id}", params={"lines": lines})

        if isinstance(response, list):
            for log in response:
                console.print(log)
        else:
            console.print(response)

    except Exception as e:
        # Fallback if endpoint not found or error
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
