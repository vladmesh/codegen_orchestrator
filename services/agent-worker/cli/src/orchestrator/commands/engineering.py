from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def trigger(project_id: int, description: str):
    """Trigger an engineering task."""
    # Engineering subgraph triggering via API is not yet implemented in Phase 2.
    # It requires Phase 6 (Graph Triggers).
    console.print(f"Triggering engineering task for project {project_id}: {description}")
    console.print(
        "[yellow]Warning: Engineering API endpoint not yet implemented (Phase 6)[/yellow]"
    )


@app.command()
def status(task_id: str):
    """Check engineering task status."""
    console.print(f"Checking status for task {task_id}")
    console.print(
        "[yellow]Warning: Engineering API endpoint not yet implemented (Phase 6)[/yellow]"
    )
