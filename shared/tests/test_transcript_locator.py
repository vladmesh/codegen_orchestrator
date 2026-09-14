"""Contract tests for durable worker transcript locators."""

import pytest

from shared.contracts.transcript import (
    TranscriptLocatorError,
    make_transcript_locator,
    validate_transcript_locator,
)


def test_locator_round_trip_is_relative_and_owned() -> None:
    locator = make_transcript_locator("qa-worker-1", "request-2")

    assert locator == "v1/qa-worker-1/request-2.log"
    assert (
        validate_transcript_locator(
            locator, expected_worker_id="qa-worker-1", expected_request_id="request-2"
        )
        == locator
    )


@pytest.mark.parametrize(
    "locator",
    [
        "/data/worker-transcripts/worker/request.log",
        "/artifacts/worker-transcripts/worker/request.log",
        "v1/worker/../request.log",
        "v1//request.log",
        "v1/worker/request.txt",
        "v2/worker/request.log",
        "v1/worker/nested/request.log",
    ],
)
def test_locator_rejects_absolute_traversing_empty_and_unsupported_paths(locator: str) -> None:
    with pytest.raises(TranscriptLocatorError):
        validate_transcript_locator(locator)


def test_locator_rejects_neighbour_worker_and_request() -> None:
    locator = make_transcript_locator("worker-a", "request-a")

    with pytest.raises(TranscriptLocatorError, match="worker"):
        validate_transcript_locator(locator, expected_worker_id="worker-b")
    with pytest.raises(TranscriptLocatorError, match="request"):
        validate_transcript_locator(locator, expected_request_id="request-b")
