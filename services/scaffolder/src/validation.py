"""Validate scaffold inputs at the exec boundary.

run_scaffold / run_ensure_workspace interpolate project_name and modules into
shell command strings (copier, git commit), so anything reaching the shell must
be free of shell metacharacters. The LLM-facing tool in services/langgraph
validates project names, but the queue consumer forwards ScaffoldMessage fields
straight through. Re-validate here, where the values actually hit the shell, so
no upstream path can inject.
"""

from __future__ import annotations

import re

# Mirrors PROJECT_NAME_PATTERN in services/langgraph/src/tools/projects.py.
_PROJECT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
# A single module token, e.g. "backend" or "tg_bot".
_MODULE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class ScaffoldInputError(ValueError):
    """Scaffold input failed validation and must not reach the shell."""


def validate_project_name(name: str) -> None:
    """Reject project names that aren't ^[a-z][a-z0-9-]*$."""
    if not _PROJECT_NAME_PATTERN.match(name):
        raise ScaffoldInputError(f"invalid project_name {name!r}: expected ^[a-z][a-z0-9-]*$")


def validate_modules(modules: str) -> None:
    """Reject a comma-separated module list with any non-^[a-z][a-z0-9_-]*$ token."""
    if modules == "":
        return
    for token in modules.split(","):
        if not _MODULE_PATTERN.match(token):
            raise ScaffoldInputError(
                f"invalid module {token!r} in modules={modules!r}: "
                "expected comma-separated ^[a-z][a-z0-9_-]*$ tokens"
            )
