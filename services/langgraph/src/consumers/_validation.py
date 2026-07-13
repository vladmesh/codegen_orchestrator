"""Validation diagnostics shared by queue consumers."""

from __future__ import annotations

from pydantic import ValidationError

from shared.diagnostics import safe_validation_errors


def _safe_validation_errors(exc: ValidationError) -> list[dict]:
    """Compatibility import for consumers sharing canonical diagnostics."""
    return safe_validation_errors(exc)
