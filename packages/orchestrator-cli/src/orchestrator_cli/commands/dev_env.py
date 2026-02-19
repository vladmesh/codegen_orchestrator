"""Dev environment infrastructure commands for orchestrator CLI."""

import asyncio
import json
import os

from rich.console import Console
import typer

from orchestrator_cli.client import get_worker_manager_client

app = typer.Typer()
console = Console()


def _get_worker_id() -> str:
    """Get the current worker ID from the environment."""
    worker_id = os.getenv("WORKER_ID")
    if not worker_id:
        raise RuntimeError("WORKER_ID is not set")
    return worker_id


async def _compose_async(
    worker_id: str, args: list[str], cwd: str = ".", timeout: int = 120
) -> dict:
    """Send a compose request to the worker manager."""
    client = get_worker_manager_client()
    payload = {"args": args, "cwd": cwd, "timeout": timeout}
    try:
        response = await client.post(f"/api/worker/{worker_id}/infra/compose", json=payload)
        response.raise_for_status()
        return response.json()
    finally:
        await client.aclose()


@app.command()
def compose(
    args: list[str] = typer.Argument(..., help="docker compose arguments (e.g. up -d db)"),
    cwd: str = typer.Option(".", "--cwd", help="Working directory relative to /workspace"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout in seconds"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Run a docker compose command in the worker's workspace."""
    try:
        worker_id = _get_worker_id()
        result = asyncio.run(_compose_async(worker_id, list(args), cwd=cwd, timeout=timeout))

        if json_output:
            typer.echo(json.dumps(result, indent=2))
            return

        if result.get("stdout"):
            console.print(result["stdout"], end="")
        if result.get("stderr"):
            console.print(result["stderr"], end="")

        exit_code = result.get("exit_code", 0)
        if exit_code != 0:
            raise typer.Exit(code=exit_code)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command()
def start_infra(
    services: list[str] = typer.Argument(default=None, help="Services to start (default: all)"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout in seconds"),
):
    """Start infrastructure services (docker compose up -d --wait)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        args = ["up", "-d", "--wait"] + list(services or [])
        result = asyncio.run(_compose_async(worker_id, args, timeout=timeout))

        if result.get("stdout"):
            console.print(result["stdout"], end="")
        if result.get("stderr"):
            console.print(result["stderr"], end="")

        exit_code = result.get("exit_code", 0)
        if exit_code == 0:
            console.print("[green]Infrastructure started.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def stop_infra(
    timeout: int = typer.Option(60, "--timeout", help="Timeout in seconds"),
):
    """Stop infrastructure services (docker compose stop)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        result = asyncio.run(_compose_async(worker_id, ["stop"], timeout=timeout))

        if result.get("stdout"):
            console.print(result["stdout"], end="")
        if result.get("stderr"):
            console.print(result["stderr"], end="")

        exit_code = result.get("exit_code", 0)
        if exit_code == 0:
            console.print("[green]Infrastructure stopped.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def reset_infra(
    timeout: int = typer.Option(120, "--timeout", help="Timeout in seconds"),
):
    """Tear down infrastructure and remove volumes (docker compose down -v)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        result = asyncio.run(_compose_async(worker_id, ["down", "-v"], timeout=timeout))

        if result.get("stdout"):
            console.print(result["stdout"], end="")
        if result.get("stderr"):
            console.print(result["stderr"], end="")

        exit_code = result.get("exit_code", 0)
        if exit_code == 0:
            console.print("[green]Infrastructure reset.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)
