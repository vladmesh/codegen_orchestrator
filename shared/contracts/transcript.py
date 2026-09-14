"""Opaque, path-safe locators for durable worker transcripts."""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Annotated

from pydantic import AfterValidator

TRANSCRIPT_LOCATOR_VERSION = "v1"
TRANSCRIPT_SUFFIX = ".log"
_LOCATOR_PARTS = 3
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class TranscriptLocatorError(ValueError):
    """A transcript locator is malformed or belongs to another turn."""


class TranscriptUnavailableReason(StrEnum):
    """Why an agent-started terminal result has no retained transcript."""

    SAVE_FAILED = "save_failed"


def _validate_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise TranscriptLocatorError(f"invalid transcript {name}")
    return value


def make_transcript_locator(worker_id: str, request_id: str) -> str:
    """Build the only locator format persisted outside worker-manager."""
    worker = _validate_component(worker_id, "worker id")
    request = _validate_component(request_id, "request id")
    return f"{TRANSCRIPT_LOCATOR_VERSION}/{worker}/{request}{TRANSCRIPT_SUFFIX}"


def validate_transcript_locator(
    locator: str,
    *,
    expected_worker_id: str | None = None,
    expected_request_id: str | None = None,
) -> str:
    """Validate syntax and, when supplied, the turn that owns a locator."""
    if not isinstance(locator, str) or locator.startswith("/") or "\\" in locator:
        raise TranscriptLocatorError("transcript locator must be a relative POSIX locator")
    parts = locator.split("/")
    if len(parts) != _LOCATOR_PARTS or parts[0] != TRANSCRIPT_LOCATOR_VERSION:
        raise TranscriptLocatorError("unsupported transcript locator")
    worker_id = _validate_component(parts[1], "worker id")
    filename = parts[2]
    if not filename.endswith(TRANSCRIPT_SUFFIX):
        raise TranscriptLocatorError("unsupported transcript suffix")
    request_id = _validate_component(filename[: -len(TRANSCRIPT_SUFFIX)], "request id")
    if expected_worker_id is not None and worker_id != expected_worker_id:
        raise TranscriptLocatorError("transcript locator belongs to another worker")
    if expected_request_id is not None and request_id != expected_request_id:
        raise TranscriptLocatorError("transcript locator belongs to another request")
    return locator


TranscriptLocator = Annotated[str, AfterValidator(validate_transcript_locator)]


def transcript_locator_parts(locator: str) -> tuple[str, str]:
    """Return the validated worker and request components."""
    validate_transcript_locator(locator)
    _, worker_id, filename = locator.split("/")
    return worker_id, filename[: -len(TRANSCRIPT_SUFFIX)]
