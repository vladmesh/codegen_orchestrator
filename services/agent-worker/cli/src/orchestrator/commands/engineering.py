from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def trigger(project_id: int, description: str):
    """Trigger an engineering task."""
    console.print(f"Triggering engineering task for project {project_id}: {description}")


@app.command()
def status(task_id: str):
    """Check engineering task status."""
    console.print(f"Checking status for task {task_id}")
