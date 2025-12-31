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
        # response = client.post(f"/projects/{project_id}/deploy")
        response = {
            "task_id": "deploy-abc1234",
            "status": "queued",
            "project_id": project_id,
        }  # Mock
        console.print("[bold green]Deployment triggered![/bold green]")
        console.print(f"Task ID: {response['task_id']}")
        console.print("Monitor status with: orchestrator deploy status <task_id>")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def status(task_id: str):
    """Get status of a deployment task."""
    try:
        # response = client.get(f"/tasks/{task_id}")
        response = {
            "task_id": task_id,
            "status": "running",
            "logs": ["Step 1: Building...", "Step 2: Deploying..."],
        }  # Mock

        console.print(f"Task: {task_id}")
        console.print(f"Status: [yellow]{response['status']}[/yellow]")
        console.print("[dim]Recent logs:[/dim]")
        for log in response.get("logs", []):
            console.print(f"  {log}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def logs(project_id: int, lines: int = 100):
    """Get deployment logs for a project."""
    try:
        console.print(f"Fetching last {lines} lines of logs for project {project_id}...")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
