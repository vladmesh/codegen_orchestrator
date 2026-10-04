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


def test_retained_empty_output_fences_launch_before_its_story_stop_commits():
    from shared.contracts.dto.run import EMPTY_RESULT_TERMINAL_KEY, EmptyEngineeringTerminal

    terminal = EmptyEngineeringTerminal.model_validate(
        {
            "status": "failed",
            "error_message": "No new commit",
            "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
        }
    ).model_dump(mode="json", exclude_unset=True)
    story = SimpleNamespace(status="in_progress", engineering_stop=None)
    run = SimpleNamespace(
        id="eng-empty",
        status="running",
        result=terminal["result"],
        run_metadata={EMPTY_RESULT_TERMINAL_KEY: terminal},
    )
    assert disposition(story, None, [run]) is AttemptDisposition.STOPPED
    # Once settled, this metadata grants no retry block to ordinary later work.
    run.status = "failed"
    assert disposition(story, None, [run]) is AttemptDisposition.ELIGIBLE
