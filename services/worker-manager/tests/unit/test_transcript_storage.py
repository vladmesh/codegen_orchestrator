"""Path-safe worker transcript storage boundary."""

import os
from pathlib import Path
import time

import pytest

from src.transcript_storage import (
    TranscriptExpired,
    TranscriptMalformed,
    TranscriptNotFound,
    TranscriptUnsafe,
    read_transcript,
    resolve_transcript,
)


def test_resolve_and_read_retained_transcript(tmp_path: Path) -> None:
    artifact = tmp_path / "worker-1" / "request-1.log"
    artifact.parent.mkdir()
    artifact.write_text("retained", encoding="utf-8")

    resolved = resolve_transcript(
        "v1/worker-1/request-1.log", storage_root=tmp_path, retention_days=7
    )

    assert resolved == artifact
    assert (
        read_transcript("v1/worker-1/request-1.log", storage_root=tmp_path, retention_days=7)
        == "retained"
    )


def test_missing_transcript_is_distinct(tmp_path: Path) -> None:
    with pytest.raises(TranscriptNotFound):
        resolve_transcript("v1/worker-1/request-1.log", storage_root=tmp_path, retention_days=7)


def test_malformed_transcript_is_distinct(tmp_path: Path) -> None:
    with pytest.raises(TranscriptMalformed):
        resolve_transcript("../secret.log", storage_root=tmp_path, retention_days=7)


def test_expired_transcript_is_distinct(tmp_path: Path) -> None:
    artifact = tmp_path / "worker-1" / "request-1.log"
    artifact.parent.mkdir()
    artifact.write_text("old", encoding="utf-8")
    old = time.time() - 3 * 86400
    os.utime(artifact, (old, old))

    with pytest.raises(TranscriptExpired):
        resolve_transcript("v1/worker-1/request-1.log", storage_root=tmp_path, retention_days=1)


def test_symlink_cannot_escape_storage_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-transcript.log"
    outside.write_text("secret", encoding="utf-8")
    worker = tmp_path / "worker-1"
    worker.mkdir()
    (worker / "request-1.log").symlink_to(outside)

    with pytest.raises(TranscriptUnsafe):
        resolve_transcript("v1/worker-1/request-1.log", storage_root=tmp_path, retention_days=7)
