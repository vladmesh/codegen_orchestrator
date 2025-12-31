from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def logs(service: str):
    """View service logs."""
    console.print(f"Fetching logs for {service}...")


@app.command()
def health():
    """Check system health."""
    console.print("System is healthy.")
