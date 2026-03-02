"""Dev environment infrastructure commands for orchestrator CLI."""

import asyncio
import json
import os
from pathlib import Path

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
    import httpx

    client = get_worker_manager_client()
    payload = {"args": args, "cwd": cwd, "timeout": timeout}
    # HTTP timeout must exceed the compose timeout to avoid premature disconnect
    http_timeout = httpx.Timeout(timeout + 30, connect=10)
    try:
        response = await client.post(
            f"/api/worker/{worker_id}/infra/compose", json=payload, timeout=http_timeout
        )
        response.raise_for_status()
        return response.json()
    finally:
        await client.aclose()


def _build_file_args(file: list[str] | None) -> list[str]:
    """Convert -f/--file options into compose args."""
    if not file:
        return []
    result = []
    for f in file:
        result.extend(["-f", f])
    return result


def _print_result(result: dict) -> int:
    """Print compose output and return exit code."""
    if result.get("stdout"):
        console.print(result["stdout"], end="")
    if result.get("stderr"):
        console.print(result["stderr"], end="")
    return result.get("exit_code", 0)


def _format_error(e: Exception) -> str:
    """Format exception for display — some exceptions (e.g. httpx.ReadTimeout) have empty str()."""
    msg = str(e)
    if msg:
        return msg
    return type(e).__name__


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

        exit_code = _print_result(result)
        if exit_code != 0:
            raise typer.Exit(code=exit_code)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {_format_error(e)}")
        raise typer.Exit(code=1) from None


@app.command()
def start_infra(
    services: list[str] = typer.Argument(default=None, help="Services to start (default: all)"),
    file: list[str] = typer.Option(None, "-f", "--file", help="Compose file(s) to use"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout in seconds"),
):
    """Start infrastructure services (docker compose up -d --wait)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        args = _build_file_args(file) + ["up", "-d", "--wait"] + list(services or [])
        result = asyncio.run(_compose_async(worker_id, args, timeout=timeout))

        exit_code = _print_result(result)
        if exit_code == 0:
            _patch_db_hostname()
            console.print("[green]Infrastructure started.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {_format_error(e)}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


def _patch_db_hostname():
    """Replace POSTGRES_HOST=db with project-db in .env to avoid DNS collision.

    The worker container is connected to both codegen_internal (where the
    orchestrator's 'db' lives) and the project's dev network. The generic
    name 'db' resolves to the orchestrator's postgres. The compose network
    override adds 'project-db' as a unique alias for the project's DB.
    """
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        return
    content = env_path.read_text()
    if "POSTGRES_HOST=db" not in content:
        return
    content = content.replace("POSTGRES_HOST=db", "POSTGRES_HOST=project-db")
    env_path.write_text(content)


@app.command()
def stop_infra(
    file: list[str] = typer.Option(None, "-f", "--file", help="Compose file(s) to use"),
    timeout: int = typer.Option(60, "--timeout", help="Timeout in seconds"),
):
    """Stop infrastructure services (docker compose stop)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        args = _build_file_args(file) + ["stop"]
        result = asyncio.run(_compose_async(worker_id, args, timeout=timeout))

        exit_code = _print_result(result)
        if exit_code == 0:
            console.print("[green]Infrastructure stopped.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {_format_error(e)}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def reset_infra(
    file: list[str] = typer.Option(None, "-f", "--file", help="Compose file(s) to use"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout in seconds"),
):
    """Tear down infrastructure and remove volumes (docker compose down -v)."""
    exit_code = 0
    try:
        worker_id = _get_worker_id()
        args = _build_file_args(file) + ["down", "-v"]
        result = asyncio.run(_compose_async(worker_id, args, timeout=timeout))

        exit_code = _print_result(result)
        if exit_code == 0:
            console.print("[green]Infrastructure reset.[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {_format_error(e)}")
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)
