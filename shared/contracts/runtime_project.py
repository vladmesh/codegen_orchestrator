"""Canonical runtime project slug validation."""

from __future__ import annotations

import re
from typing import Any

RUNTIME_PROJECT_SLUG_PATTERN = r"^[a-z][a-z0-9-]*$"
_RUNTIME_PROJECT_SLUG_RE = re.compile(RUNTIME_PROJECT_SLUG_PATTERN)


class RuntimeProjectSlug(str):
    """Runtime-safe project identifier used in paths, compose names and SSH commands."""

    def __new__(cls, value: Any) -> RuntimeProjectSlug:
        return str.__new__(cls, runtime_project_slug(value))


def runtime_project_slug(value: Any) -> RuntimeProjectSlug:
    """Return a validated runtime project slug or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError(
            f"invalid runtime project slug {value!r}: expected {RUNTIME_PROJECT_SLUG_PATTERN}"
        )
    if not _RUNTIME_PROJECT_SLUG_RE.fullmatch(value):
        raise ValueError(
            f"invalid runtime project slug {value!r}: expected {RUNTIME_PROJECT_SLUG_PATTERN}"
        )
    return str.__new__(RuntimeProjectSlug, value)
