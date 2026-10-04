from types import SimpleNamespace

from shared.contracts.dto.commit_publication import (
    AttemptDisposition,
    CommitPublication,
    PublicationFailure,
)
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from src.attempt_disposition import disposition


def test_operator_stop_outranks_publication_and_failed_retry():
    result = EngineeringRunResult(
        engineering_status=EngineeringStatus.FAILED,
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=CommitPublication(failure=PublicationFailure.PUSH_REFUSED),
    )
    story = SimpleNamespace(status="waiting_human_review", engineering_stop=None)
    assert (
        disposition(
            story, None, [SimpleNamespace(id="eng-fixture", result=result, run_metadata={})]
        )
        is AttemptDisposition.STOPPED
    )


def test_publication_refusal_never_takes_paid_retry():
    result = EngineeringRunResult(
        engineering_status=EngineeringStatus.FAILED,
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=CommitPublication(failure=PublicationFailure.PUSH_REFUSED),
    )
    story = SimpleNamespace(status="in_progress", engineering_stop=None)
    assert (
        disposition(
            story, None, [SimpleNamespace(id="eng-fixture", result=result, run_metadata={})]
        )
        is AttemptDisposition.PUBLICATION_REQUIRED
    )


def test_ordinary_failure_retains_bounded_retry():
    story = SimpleNamespace(status="in_progress", engineering_stop=None)
    assert disposition(story, None, []) is AttemptDisposition.ELIGIBLE
