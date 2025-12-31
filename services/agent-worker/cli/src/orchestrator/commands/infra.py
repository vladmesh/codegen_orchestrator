from rich.console import Console
import typer

from orchestrator.client import APIClient

app = typer.Typer()
console = Console()
client = APIClient()


@app.command()
def list():
    """List infrastructure resources."""
    try:
        console.print("[bold]Servers:[/bold]")
        servers = client.get("/api/servers")
        for s in servers:
            console.print(f"  {s['handle']} ({s['public_ip']}) - {s['status']}")

        console.print("\n[bold]Allocations:[/bold]")
        allocations = client.get("/api/allocations")
        for a in allocations:
            console.print(
                f"  ID: {a['id']} | Port: {a['port']} | Server: {a['server_handle']} | "
                f"Service: {a['service_name']}"
            )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def allocate(server: str, port: int, service: str, project_id: int):
    """Allocate a new resource (port)."""
    try:
        payload = {"port": port, "service_name": service, "project_id": project_id}
        res = client.post(f"/api/servers/{server}/ports", json=payload)
        console.print(
            f"[bold green]Allocated port {res['port']} on {res['server_handle']}[/bold green]"
        )
        console.print(f"Allocation ID: {res['id']}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")


@app.command()
def release(allocation_id: int):
    """Release a resource (allocation)."""
    try:
        client.delete(f"/api/allocations/{allocation_id}")
        console.print(f"[bold green]Released allocation {allocation_id}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
