"""A QA Run's first committed terminal state wakes QA routing and the temporary-access sweep.

A storyless QA Run has no story to route, so it wakes only the access sweep.
"""

import asyncio
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs
from wake_doubles import NativeRedis, RunSession, patch_run

from shared.contracts.dto.engineering_attempt import QAAccountingFact
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult, QAFailedCheck, QARunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Run

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = "qa-wake"
STORY_ID = "story-qa-wake"
QA_ROUTING = SchedulerWakeTarget.QA_ROUTING
TEMPORARY_ACCESS = SchedulerWakeTarget.TEMPORARY_ACCESS
PASSED = QARunResult(qa_outcome=QAOutcome.PASSED, summary="all green").model_dump(mode="json")
FAILED = QARunResult(
    qa_outcome=QAOutcome.FAILED,
    failed_checks=[QAFailedCheck(name="GET /health", detail="returned 503")],
).model_dump(mode="json")
BLOCKED = {"qa_outcome": "blocked", "summary": "temporary access could not be granted"}
COMMITTED = [("commit",), ("refresh",)]
BOTH = [("commit",), ("wake", QA_ROUTING), ("wake", TEMPORARY_ACCESS), ("refresh",)]
CLEANUP_ONLY = [("commit",), ("wake", TEMPORARY_ACCESS), ("refresh",)]

# Every first terminal write a QA writer makes, including a cancellation without a result.
TERMINAL_WRITES = [
    pytest.param(
        {
            "status": RunStatus.COMPLETED,
            "result": PASSED,
            "qa_accounting": QAAccountingFact(executor_started=False),
        },
        id="passed",
    ),
    pytest.param({"status": RunStatus.COMPLETED, "result": FAILED}, id="failed_verdict"),
    pytest.param(
        {"status": RunStatus.FAILED, "result": BLOCKED, "error_message": "grant failed"},
        id="failed",
    ),
    pytest.param({"status": RunStatus.CANCELLED}, id="cancelled_without_result"),
]
LIVE_STATUSES = [RunStatus.QUEUED.value, RunStatus.RUNNING.value]


def make_run(
    *,
    status: str = RunStatus.RUNNING.value,
    result: dict | None = None,
    story_id: str | None = STORY_ID,
    type: str = RunType.QA.value,
    metadata: dict | None = None,
) -> Run:
    return Run(
        id=RUN_ID,
        type=type,
        status=status,
        project_id=PROJECT_ID,
        story_id=story_id,
        task_id=None,
        run_metadata={} if metadata is None else metadata,
        result=result,
        completed_at=None,
    )


def settled(**fields) -> Run:
    return make_run(status=RunStatus.COMPLETED.value, result=PASSED, **fields)


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def native(journal) -> NativeRedis:
    return NativeRedis(journal)


@pytest.fixture
def redis(native) -> SimpleNamespace:
    return SimpleNamespace(redis=native)


def pass_qa(session: RunSession, redis):
    return patch_run(session, redis, status=RunStatus.COMPLETED, result=PASSED)


@pytest.mark.asyncio
@pytest.mark.parametrize("live", LIVE_STATUSES)
@pytest.mark.parametrize("fields", TERMINAL_WRITES)
async def test_a_first_terminal_state_wakes_routing_and_cleanup_between_commit_and_refresh(
    journal, redis, live, fields
):
    updated = await patch_run(RunSession(journal, make_run(status=live)), redis, **fields)

    assert updated.status == fields["status"]
    assert journal == BOTH


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", TERMINAL_WRITES)
async def test_a_storyless_qa_run_wakes_only_its_cleanup(journal, redis, fields):
    await patch_run(RunSession(journal, make_run(story_id=None)), redis, **fields)

    assert journal == CLEANUP_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "expected"),
    [
        pytest.param(make_run(), BOTH, id="story"),
        pytest.param(
            make_run(
                story_id=None,
                metadata={
                    "temporary_access_grant_id": "grant-qa-wake",
                    "temporary_access_operation": "grant",
                },
            ),
            CLEANUP_ONLY,
            id="storyless_with_grant_marker",
        ),
    ],
)
async def test_a_deploy_shaped_qa_terminal_write_wakes_only_the_qa_owners(
    journal, redis, run, expected
):
    success = DeployRunResult(deploy_outcome=DeployOutcome.SUCCESS).model_dump(mode="json")

    await patch_run(RunSession(journal, run), redis, status=RunStatus.COMPLETED, result=success)

    assert journal == expected


@pytest.mark.asyncio
async def test_nothing_is_published_while_the_commit_is_held(journal, redis):
    session = RunSession(journal, make_run())
    session.hold = asyncio.Event()

    request = asyncio.create_task(pass_qa(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    await asyncio.sleep(0)
    assert journal == []

    session.hold.set()
    await request
    assert journal == BOTH


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = RunSession(journal, make_run())
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await pass_qa(session, redis)

    assert journal == []


@pytest.mark.asyncio
async def test_a_result_written_while_running_waits_for_the_terminal_status(journal, redis):
    session = RunSession(journal, make_run())

    await patch_run(session, redis, result=PASSED)
    assert journal == COMMITTED

    journal.clear()
    updated = await patch_run(session, redis, status=RunStatus.COMPLETED)
    assert updated.result == PASSED
    assert journal == BOTH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields"),
    [
        pytest.param(
            settled(),
            {"status": RunStatus.COMPLETED, "result": PASSED},
            id="identical_replay",
        ),
        pytest.param(
            settled(),
            {
                "status": RunStatus.COMPLETED,
                "qa_accounting": QAAccountingFact(executor_started=False),
            },
            id="accounting_replay",
        ),
        pytest.param(settled(), {"run_metadata": {"transcript": "kept"}}, id="settled_metadata"),
        pytest.param(
            make_run(status=RunStatus.CANCELLED.value),
            {"status": RunStatus.CANCELLED},
            id="cancellation_replay",
        ),
        pytest.param(make_run(), {"run_metadata": {"stage": "probing"}}, id="live_metadata"),
        pytest.param(
            make_run(status=RunStatus.QUEUED.value),
            {"status": RunStatus.RUNNING},
            id="nonterminal",
        ),
        pytest.param(
            make_run(type=RunType.DEPLOY.value),
            {"status": RunStatus.CANCELLED},
            id="deploy_run",
        ),
        pytest.param(
            make_run(type=RunType.DEPLOY.value, story_id=None),
            {"status": RunStatus.FAILED, "error_message": "build failed"},
            id="storyless_deploy_run",
        ),
    ],
)
async def test_a_write_that_does_not_first_end_a_qa_run_publishes_nothing(
    journal, redis, run, fields
):
    await patch_run(RunSession(journal, run), redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields", "status_code"),
    [
        pytest.param(
            make_run(),
            {"internal": False, "status": RunStatus.COMPLETED, "result": PASSED},
            401,
            id="unauthenticated",
        ),
        pytest.param(
            make_run(),
            {
                "status": RunStatus.RUNNING,
                "qa_accounting": QAAccountingFact(executor_started=False),
            },
            422,
            id="accounting_without_terminal_status",
        ),
        pytest.param(
            settled(),
            {"status": RunStatus.COMPLETED, "result": FAILED},
            409,
            id="verdict_rewrite",
        ),
        pytest.param(settled(), {"status": RunStatus.RUNNING}, 409, id="resurrection"),
    ],
)
async def test_a_refused_update_publishes_nothing(journal, redis, run, fields, status_code):
    with pytest.raises(HTTPException) as refused:
        await patch_run(RunSession(journal, run), redis, **fields)

    assert refused.value.status_code == status_code
    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [{"status": RunStatus.COMPLETED}, {}], ids=["status", "result"])
async def test_a_late_verdict_after_a_cancellation_is_refused_without_a_hint(journal, redis, late):
    session = RunSession(journal, make_run())

    await patch_run(session, redis, status=RunStatus.CANCELLED)
    assert journal == BOTH

    journal.clear()
    with pytest.raises(HTTPException) as refused:
        await patch_run(session, redis, result=PASSED, **late)

    assert refused.value.status_code == 409
    assert journal == []
    assert session.run.status == RunStatus.CANCELLED
    assert session.run.result is None


@pytest.mark.asyncio
async def test_zero_subscribers_is_a_published_hint(journal, redis, native):
    native.receivers = 0

    with capture_logs() as logs:
        await pass_qa(RunSession(journal, make_run()), redis)

    assert journal == BOTH
    published = [log for log in logs if log["event"].endswith("_wake_published")]
    assert [(log["event"], log["receivers"]) for log in published] == [
        ("qa_routing_wake_published", 0),
        ("temporary_access_wake_published", 0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "attempted"),
    [(QA_ROUTING, TEMPORARY_ACCESS), (TEMPORARY_ACCESS, QA_ROUTING)],
    ids=["routing_lost", "cleanup_lost"],
)
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_one_lost_hint_leaves_the_other_attempted_and_the_response_unchanged(
    journal, redis, native, failing, attempted, failure
):
    native.failures[failing] = failure

    with capture_logs() as logs:
        updated = await pass_qa(RunSession(journal, make_run()), redis)

    assert updated.status == RunStatus.COMPLETED
    assert updated.result == PASSED
    assert journal == [("commit",), ("wake", attempted), ("refresh",)]
    [failed] = [log for log in logs if log["event"].endswith("_wake_failed")]
    assert failed == {
        "event": f"{failing.value}_wake_failed",
        "log_level": "warning",
        "run_id": RUN_ID,
        "story_id": STORY_ID,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_propagates_after_the_commit(journal, redis, native):
    native.hold = asyncio.Event()

    request = asyncio.create_task(pass_qa(RunSession(journal, make_run()), redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit",)]
