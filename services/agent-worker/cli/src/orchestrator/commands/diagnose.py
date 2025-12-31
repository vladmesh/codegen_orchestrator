import http

from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def logs(service: str):
    """View service logs."""
    # This might need a specific endpoint or integration with Loki/etc.
    # For now, we'll just check if there's an endpoint or leave as TODO
    console.print(f"Fetching logs for {service}... (Not implemented on API yet)")


@app.command()
def health():
    """Check system health."""
    try:
        # Assuming there is a general health endpoint, usually at root or /health
        # The client base URL is /api/v1 usually, but health might be at /health
        # Let's try /health relative to base URL if configured, or just skip if not standard.
        # But wait, main.py usually has a health check.
        # Checking main.py: app.get("/health")
        # Client base_url is http://api:8000
        res = client.client.get(f"{client.base_url}/health")
        if res.status_code == http.HTTPStatus.OK:
            console.print("[green]System is healthy.[/green]")
            console.print(res.json())
        else:
            console.print(f"[red]System health check failed: {res.status_code}[/red]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def incidents(active_only: bool = True):
    """List incidents."""
    try:
        endpoint = "/api/incidents/active" if active_only else "/api/incidents"
        incidents = client.get(endpoint)
        if not incidents:
            console.print("No incidents found.")
        for inc in incidents:
            console.print(
                f"[{inc['status']}] {inc['incident_type']} on "
                f"{inc['server_handle']}: {inc['details']}"
            )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
