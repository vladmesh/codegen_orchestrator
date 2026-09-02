"""Filesystem helpers shared across generators."""

import ast
import os
from pathlib import Path
import tempfile

GENERATED_FILE_MODE = 0o644


def parse_python(path: Path) -> ast.Module | None:
    """Parse a Python file into an AST module, or None if it can't be read or parsed.

    Shared by the AST-based linters so they swallow unreadable/unparseable files
    the same way instead of each catching a different exception set.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace *path* so generated files stay complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, GENERATED_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
