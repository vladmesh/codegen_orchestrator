import http
import json as json_lib

from rich.console import Console
import typer

from orchestrator.client import APIClient
from orchestrator.permissions import require_permission

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
@require_permission("diagnose")
def logs(service: str):
    """View service logs."""
    # This might need a specific endpoint or integration with Loki/etc.
    # For now, we'll just check if there's an endpoint or leave as TODO
    console.print(f"Fetching logs for {service}... (Not implemented on API yet)")


@app.command()
@require_permission("diagnose")
def health(json_output: bool = typer.Option(False, "--json", help="Output as JSON")):
    """Check system health."""
    try:
        res = client.client.get(f"{client.base_url}/health")
        if res.status_code == http.HTTPStatus.OK:
            data = res.json()
            if json_output:
                typer.echo(json_lib.dumps(data, indent=2))
                return
            console.print("[green]System is healthy.[/green]")
            console.print(data)
        else:
            if json_output:
                typer.echo(
                    json_lib.dumps({"status": "unhealthy", "code": res.status_code}, indent=2)
                )
                return
            console.print(f"[red]System health check failed: {res.status_code}[/red]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
@require_permission("diagnose")
def incidents(
    active_only: bool = True,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List incidents."""
    try:
        endpoint = "/api/incidents/active" if active_only else "/api/incidents"
        incidents_list = client.get(endpoint)

        if json_output:
            typer.echo(json_lib.dumps(incidents_list, indent=2))
            return

        if not incidents_list:
            console.print("No incidents found.")
            return

        for inc in incidents_list:
            console.print(
                f"[{inc['status']}] {inc['incident_type']} on "
                f"{inc['server_handle']}: {inc['details']}"
            )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
