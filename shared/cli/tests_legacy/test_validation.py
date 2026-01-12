"""Unit tests for Pydantic validation decorator."""

from orchestrator.validation import validate
from pydantic import BaseModel, Field
import typer
from typer.testing import CliRunner

runner = CliRunner()


class SimpleModel(BaseModel):
    """Test model for validation."""

    name: str = Field(..., min_length=3, max_length=10)
    count: int = Field(default=5, ge=0, le=100)


def test_validate_decorator_with_valid_input():
    """Validation passes with valid input."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo(f"name={name}, count={count}")

    result = runner.invoke(app, ["hello", "--count", "10"])

    assert result.exit_code == 0
    assert "name=hello, count=10" in result.output


def test_validate_decorator_with_defaults():
    """Validation uses default values when not provided."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo(f"name={name}, count={count}")

    result = runner.invoke(app, ["test"])

    assert result.exit_code == 0
    assert "name=test, count=5" in result.output  # count defaults to 5


def test_validate_decorator_with_invalid_input_min_length():
    """Validation fails with input below minimum length."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo(f"name={name}")

    result = runner.invoke(app, ["ab"])  # Too short

    assert result.exit_code == 1
    assert "✗ name:" in result.output
    assert "at least 3 characters" in result.output.lower()


def test_validate_decorator_with_invalid_input_max_value():
    """Validation fails with input above maximum value."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo(f"count={count}")

    result = runner.invoke(app, ["valid", "--count", "200"])  # Too large

    assert result.exit_code == 1
    assert "✗ count:" in result.output
    assert "less than or equal to 100" in result.output.lower()


def test_validate_decorator_preserves_extra_kwargs():
    """Validation preserves kwargs not in the model."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
        json_output: bool = typer.Option(False, "--json-output"),
    ):
        if json_output:
            typer.echo(f"JSON: name={name}")
        else:
            typer.echo(f"TEXT: name={name}")

    result = runner.invoke(app, ["hello", "--json-output"])

    assert result.exit_code == 0
    assert "JSON: name=hello" in result.output


def test_validate_decorator_with_multiple_validation_errors():
    """Multiple validation errors are all displayed."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str,
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo("success")

    result = runner.invoke(app, ["ab", "--count", "200"])  # Both invalid

    assert result.exit_code == 1
    assert "✗ name:" in result.output
    assert "✗ count:" in result.output


def test_validate_decorator_with_missing_required_field():
    """Validation fails when required field is missing."""
    app = typer.Typer()

    @app.command()
    @validate(SimpleModel)
    def test_command(
        name: str = typer.Option(..., "--name"),  # Required option
        count: int = typer.Option(5, "--count", "-c"),
    ):
        typer.echo("success")

    result = runner.invoke(app, ["--count", "10"])  # Missing required 'name'

    # Typer validates required options before our decorator runs
    assert result.exit_code == 2  # noqa: PLR2004
    assert "Missing option" in result.output or "--name" in result.output
