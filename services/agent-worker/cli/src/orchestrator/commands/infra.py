from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def list():
    """List infrastructure resources."""
    console.print("Listing infrastructure...")


@app.command()
def allocate(resource_type: str):
    """Allocate a new resource."""
    console.print(f"Allocating {resource_type}...")
