"""Validation diagnostics shared by queue consumers."""

from __future__ import annotations

from pydantic import ValidationError


def _safe_validation_errors(exc: ValidationError) -> list[dict]:
    """Return validation diagnostics without values from the Redis entry."""
    return [
        {"type": error["type"], "loc": list(error["loc"])}
        for error in exc.errors(include_url=False, include_input=False)
    ]
