"""Shared helpers for introspection routers."""

import os
from http import HTTPStatus
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

MAX_FILE_SIZE = 1_000_000  # 1 MB


class FileTreeEntry(BaseModel):
    path: str
    is_dir: bool
    size: int


def safe_resolve(workspace: Path, relative_path: str) -> Path:
    """Resolve path safely, raising 403 on traversal attempts."""
    resolved = (workspace / relative_path).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Path traversal not allowed",
        )
    return resolved


SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", ".mypy_cache", ".ruff_cache"}


def walk_workspace(workspace: Path) -> list[FileTreeEntry]:
    """Walk workspace directory and return flat list of file tree entries."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(workspace)
        if str(rel_dir) != ".":
            entries.append(FileTreeEntry(path=str(rel_dir), is_dir=True, size=0))
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(workspace)
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            entries.append(FileTreeEntry(path=str(rel), is_dir=False, size=size))
    return entries


def read_file(workspace: Path, file_path: str) -> tuple[str, int]:
    """Read a file from workspace, returning (content, size).

    Raises HTTPException on traversal, not found, too large, or binary.
    """
    resolved = safe_resolve(workspace, file_path)

    if not resolved.exists():
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="File not found")

    if not resolved.is_file():
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Path is not a regular file",
        )

    size = resolved.stat().st_size
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=f"File too large ({size} bytes, max {MAX_FILE_SIZE})",
        )

    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Binary file cannot be read as text",
        )

    return content, size
