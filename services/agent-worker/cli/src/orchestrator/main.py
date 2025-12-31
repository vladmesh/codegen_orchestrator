from rich.console import Console
import typer

from orchestrator.commands import deploy, diagnose, engineering, infra, project

app = typer.Typer(
    name="orchestrator",
    help="CLI for CodeGen Orchestrator Agent",
    add_completion=False,
)
console = Console()

# Register sub-commands
app.add_typer(project.app, name="project", help="Manage projects")
app.add_typer(deploy.app, name="deploy", help="Manage deployments")
app.add_typer(infra.app, name="infra", help="Manage infrastructure")
app.add_typer(engineering.app, name="engineering", help="Engineering tasks")
app.add_typer(diagnose.app, name="diagnose", help="System diagnosis")


@app.command()
def respond(message: str):
    """Send a response back to the user."""
    # This is a bit unique. In the agent architecture, the "response"
    # is usually just the output of the CLI command.
    # But if LLM wants to explicitly talk to user, it can use this.
    console.print(f"[green]Response sent:[/green] {message}")


@app.command()
def search(query: str):
    """Search knowledge base."""
    console.print(f"[yellow]Searching for:[/yellow] {query}")
    # TODO: Implement vector store search via API
    console.print("No results found (Not implemented yet)")


@app.command()
def finish(summary: str):
    """Mark the current task as finished."""
    console.print("[bold green]Task Finished![/bold green]")
    console.print(f"Summary: {summary}")


if __name__ == "__main__":
    app()
