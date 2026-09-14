"""The only translation and read boundary for retained worker transcripts."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time

from shared.contracts.transcript import (
    TranscriptLocatorError,
    transcript_locator_parts,
    validate_transcript_locator,
)


class TranscriptStorageError(RuntimeError):
    """Base error for unavailable transcript evidence."""


class TranscriptNotFound(TranscriptStorageError):
    """A valid locator has no retained file."""


class TranscriptExpired(TranscriptStorageError):
    """The retained file is beyond the configured retention window."""


class TranscriptUnsafe(TranscriptStorageError):
    """The locator resolves through an unsafe filesystem object."""


class TranscriptMalformed(TranscriptStorageError):
    """The locator does not satisfy the shared wire contract."""


def resolve_transcript(locator: str, *, storage_root: str | Path, retention_days: int) -> Path:
    """Resolve one locator without following symlinks or leaving the storage root."""
    try:
        validate_transcript_locator(locator)
        worker_id, request_id = transcript_locator_parts(locator)
    except TranscriptLocatorError as exc:
        raise TranscriptMalformed("malformed transcript locator") from exc
    root = Path(storage_root)
    worker_dir = root / worker_id
    artifact = worker_dir / f"{request_id}.log"

    if root.is_symlink() or worker_dir.is_symlink() or artifact.is_symlink():
        raise TranscriptUnsafe("transcript locator crosses a symlink")
    try:
        root_resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise TranscriptNotFound("transcript storage root does not exist") from exc
    try:
        artifact_resolved = artifact.resolve(strict=True)
        artifact_resolved.relative_to(root_resolved)
    except FileNotFoundError as exc:
        raise TranscriptNotFound("transcript is not retained") from exc
    except ValueError as exc:
        raise TranscriptUnsafe("transcript locator escapes storage root") from exc

    mode = artifact.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise TranscriptUnsafe("transcript locator does not name a regular file")
    if artifact.stat().st_mtime < time.time() - retention_days * 86400:
        raise TranscriptExpired("transcript retention has expired")
    return artifact


def read_transcript(locator: str, *, storage_root: str | Path, retention_days: int) -> str:
    """Read exactly one validated retained transcript, never a directory or neighbour."""
    resolve_transcript(locator, storage_root=storage_root, retention_days=retention_days)
    worker_id, request_id = transcript_locator_parts(locator)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(storage_root, directory_flags)
        try:
            worker_fd = os.open(worker_id, directory_flags, dir_fd=root_fd)
            try:
                artifact_fd = os.open(
                    f"{request_id}.log",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=worker_fd,
                )
                metadata = os.fstat(artifact_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    os.close(artifact_fd)
                    raise TranscriptUnsafe("transcript locator does not name a regular file")
                if metadata.st_mtime < time.time() - retention_days * 86400:
                    os.close(artifact_fd)
                    raise TranscriptExpired("transcript retention has expired")
                with os.fdopen(artifact_fd, encoding="utf-8") as stream:
                    return stream.read()
            finally:
                os.close(worker_fd)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise TranscriptUnsafe("transcript path changed during read") from exc
