"""Pydantic validation decorator for CLI commands."""

from collections.abc import Callable
from typing import Any, TypeVar

from makefun import wraps
from pydantic import BaseModel, ValidationError
import typer

F = TypeVar("F", bound=Callable[..., Any])


def validate(model_class: type[BaseModel]) -> Callable[[F], F]:
    """Decorator for automatic Pydantic validation of CLI command arguments.

    Uses makefun.wraps to properly preserve function signature for Typer/Click.
    This ensures that Typer can correctly parse command-line arguments and generate
    proper help text.

    This decorator extracts kwargs that match the Pydantic model fields,
    validates them with Pydantic, and calls the original function with
    the validated fields expanded back as kwargs.

    The decorated function signature must match the model fields exactly.

    Args:
        model_class: Pydantic model class to validate against

    Returns:
        Decorated function that validates inputs before execution

    Example:
        @app.command()
        @validate(ProjectCreate)
        def create_project(name: str, json_output: bool = False):
            # name is validated by ProjectCreate model
            print(name)
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(**kwargs: Any) -> Any:
            try:
                # Extract only fields that belong to the model
                model_fields = model_class.model_fields.keys()
                model_data = {k: v for k, v in kwargs.items() if k in model_fields}

                # Validate with Pydantic
                validated = model_class(**model_data)

                # Call original function with validated fields + remaining kwargs
                all_kwargs = validated.model_dump()
                remaining_kwargs = {k: v for k, v in kwargs.items() if k not in model_fields}
                all_kwargs.update(remaining_kwargs)

                return func(**all_kwargs)

            except ValidationError as e:
                # Pretty print validation errors
                for err in e.errors():
                    loc = ".".join(str(x) for x in err["loc"])
                    typer.echo(f"✗ {loc}: {err['msg']}", err=True)
                raise typer.Exit(1) from e

        return wrapper  # type: ignore

    return decorator
