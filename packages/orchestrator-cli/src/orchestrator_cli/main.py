import typer

from orchestrator_cli.commands import project

app = typer.Typer()


@app.callback()
def callback():
    """
    Orchestrator CLI
    """


app.add_typer(project.app, name="project")
