import typer

from orchestrator_cli.commands import deploy, engineering, project
from orchestrator_cli.commands.respond import respond

app = typer.Typer(
    name="orchestrator",
    help="CLI for CodeGen Orchestrator",
    add_completion=False,
)


@app.callback()
def callback():
    """Orchestrator CLI - manage projects, engineering tasks, and deployments."""


# Register sub-commands
app.add_typer(project.app, name="project", help="Manage projects")
app.add_typer(engineering.app, name="engineering", help="Engineering tasks")
app.add_typer(deploy.app, name="deploy", help="Manage deployments")

# Register respond as top-level command
app.command()(respond)
