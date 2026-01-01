import json as json_lib

from rich.console import Console
import typer

from orchestrator.client import APIClient
from orchestrator.permissions import require_permission

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
@require_permission("infra")
def list(json_output: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List infrastructure resources."""
    try:
        servers = client.get("/api/servers")
        allocations = client.get("/api/allocations")

        if json_output:
            typer.echo(json_lib.dumps({"servers": servers, "allocations": allocations}, indent=2))
            return

        console.print("[bold]Servers:[/bold]")
        for s in servers:
            console.print(f"  {s['handle']} ({s['public_ip']}) - {s['status']}")

        console.print("\n[bold]Allocations:[/bold]")
        for a in allocations:
            console.print(
                f"  ID: {a['id']} | Port: {a['port']} | Server: {a['server_handle']} | "
                f"Service: {a['service_name']}"
            )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
@require_permission("infra")
def allocate(
    server: str,
    port: int,
    service: str,
    project_id: int,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Allocate a new resource (port)."""
    try:
        payload = {"port": port, "service_name": service, "project_id": project_id}
        res = client.post(f"/api/servers/{server}/ports", json=payload)

        if json_output:
            typer.echo(json_lib.dumps(res, indent=2))
            return

        console.print(
            f"[bold green]Allocated port {res['port']} on {res['server_handle']}[/bold green]"
        )
        console.print(f"Allocation ID: {res['id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
@require_permission("infra")
def release(
    allocation_id: int,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Release a resource (allocation)."""
    try:
        client.delete(f"/api/allocations/{allocation_id}")

        if json_output:
            typer.echo(
                json_lib.dumps({"allocation_id": allocation_id, "status": "released"}, indent=2)
            )
            return

        console.print(f"[bold green]Released allocation {allocation_id}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
