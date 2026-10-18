"""A deploy Run's first committed typed success wakes the sweep that owns that proof.

An ordinary story deploy wakes lifecycle supervision; a temporary-access grant or revoke
operation wakes the temporary-access sweep.
"""

import asyncio
from functools import partial
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs
from wake_doubles import NativeRedis, RunSession, patch_run

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult, DeploySkipReason
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Run

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = "deploy-wake"
STORY_ID = "story-wake"
GRANT_ID = "grant-wake"
LIFECYCLE_SUPERVISION = SchedulerWakeTarget.LIFECYCLE_SUPERVISION
TEMPORARY_ACCESS = SchedulerWakeTarget.TEMPORARY_ACCESS
WAKE = ("wake", LIFECYCLE_SUPERVISION)
SUCCESS = DeployRunResult(
    deploy_outcome=DeployOutcome.SUCCESS, deployed_url="https://wake.example"
).model_dump(mode="json")
SKIPPED = DeployRunResult(
    deploy_outcome=DeployOutcome.SUCCESS,
    skipped_reason=DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA,
).model_dump(mode="json")
CODE_FIX = DeployRunResult(
    deploy_outcome=DeployOutcome.CODE_FIX, error_details="build failed"
).model_dump(mode="json")
COMMITTED = [("commit",), ("refresh",)]
WOKEN = [("commit",), WAKE, ("refresh",)]


def make_run(
    *,
    status: str = RunStatus.RUNNING.value,
    result: dict | None = None,
    story_id: str | None = STORY_ID,
    metadata: dict | None = None,
) -> Run:
    return Run(
        id=RUN_ID,
        type=RunType.DEPLOY.value,
        status=status,
        project_id=PROJECT_ID,
        story_id=story_id,
        task_id=None,
        run_metadata={} if metadata is None else metadata,
        result=result,
        completed_at=None,
    )


def marker(operation: str = "grant", grant_id=GRANT_ID) -> dict:
    return {"temporary_access_grant_id": grant_id, "temporary_access_operation": operation}


def operation_run(operation: str = "grant", **fields) -> Run:
    """A storyless grant or revoke Run as the temporary-access sweep persists it."""
    return make_run(story_id=None, metadata=marker(operation), **fields)


def woken(target: SchedulerWakeTarget) -> list:
    return [("commit",), ("wake", target), ("refresh",)]


# Each owner's first proof, with the hint it publishes.
PROOFS = [
    pytest.param(make_run, LIFECYCLE_SUPERVISION, id="story_deploy"),
    pytest.param(operation_run, TEMPORARY_ACCESS, id="grant_operation"),
]


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def native(journal) -> NativeRedis:
    return NativeRedis(journal)


@pytest.fixture
def redis(native) -> SimpleNamespace:
    return SimpleNamespace(redis=native)


def succeed(session: RunSession, redis):
    return patch_run(session, redis, status=RunStatus.COMPLETED, result=SUCCESS)


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
async def test_a_first_deploy_success_publishes_one_hint_after_its_commit_and_before_the_refresh(
    journal, redis, run, target
):
    updated = await succeed(RunSession(journal, run()), redis)

    assert updated.status == RunStatus.COMPLETED
    assert journal == woken(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("story_id", [None, STORY_ID], ids=["storyless", "story"])
async def test_a_proved_grant_or_revoke_operation_wakes_only_the_temporary_access_sweep(
    journal, redis, operation, story_id
):
    run = make_run(story_id=story_id, metadata=marker(operation))

    await succeed(RunSession(journal, run), redis)

    assert journal == woken(TEMPORARY_ACCESS)


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
async def test_nothing_is_published_while_the_commit_is_held(journal, redis, run, target):
    session = RunSession(journal, run())
    session.hold = asyncio.Event()

    request = asyncio.create_task(succeed(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    await asyncio.sleep(0)
    assert journal == []

    session.hold.set()
    await request
    assert journal == woken(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
async def test_a_failed_commit_publishes_nothing(journal, redis, run, target):
    session = RunSession(journal, run())
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await succeed(session, redis)

    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"result": SUCCESS}, {"status": RunStatus.COMPLETED}),
        ({"status": RunStatus.COMPLETED}, {"result": SUCCESS}),
    ],
    ids=["result_then_status", "status_then_result"],
)
async def test_a_split_write_publishes_when_its_second_half_completes_the_proof(
    journal, redis, run, target, first, second
):
    session = RunSession(journal, run())

    await patch_run(session, redis, **first)
    assert journal == COMMITTED

    journal.clear()
    await patch_run(session, redis, **second)
    assert journal == woken(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, RunStatus.COMPLETED])
async def test_a_late_success_of_a_cancelled_deploy_publishes_its_hint(journal, redis, status):
    session = RunSession(journal, make_run(status=RunStatus.CANCELLED.value))
    fields = {"result": SUCCESS} if status is None else {"result": SUCCESS, "status": status}

    run = await patch_run(session, redis, **fields)

    assert run.result == SUCCESS
    assert journal == WOKEN


@pytest.mark.asyncio
async def test_a_late_result_keeps_a_cancelled_operation_unproved(journal, redis):
    session = RunSession(journal, operation_run(status=RunStatus.CANCELLED.value))

    run = await patch_run(session, redis, result=SUCCESS)

    assert run.result == SUCCESS
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run",
    [
        partial(make_run, status=RunStatus.COMPLETED.value, result=SUCCESS),
        partial(operation_run, status=RunStatus.COMPLETED.value, result=SUCCESS),
        partial(operation_run, "revoke", status=RunStatus.COMPLETED.value, result=SUCCESS),
    ],
    ids=["story_deploy", "grant_operation", "revoke_operation"],
)
@pytest.mark.parametrize(
    "fields",
    [
        {"status": RunStatus.COMPLETED, "result": SUCCESS},
        {"run_metadata": {"deployed_by": "deployer"}},
    ],
    ids=["identical_replay", "metadata_only"],
)
async def test_a_write_to_a_proven_deploy_publishes_nothing(journal, redis, run, fields):
    session = RunSession(journal, run())

    await patch_run(session, redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "update"),
    [
        ({"temporary_access_grant_id": "grant-1"}, {}),
        ({"temporary_access_operation": None}, {}),
        ({}, {"run_metadata": {"temporary_access_grant_id": None}}),
        ({"temporary_access_operation": "revoke"}, {"run_metadata": None}),
        (
            {"temporary_access_grant_id": "grant-1"},
            {"run_metadata": {"temporary_access_grant_id": None}},
        ),
    ],
    ids=["stored_grant", "stored_null_operation", "added_null_grant", "cleared", "nulled"],
)
async def test_a_success_under_an_incomplete_operation_marker_publishes_nothing(
    journal, redis, stored, update
):
    session = RunSession(journal, make_run(metadata=stored))

    run = await patch_run(session, redis, status=RunStatus.COMPLETED, result=SUCCESS, **update)

    assert run.result == SUCCESS
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "update"),
    [
        (marker(grant_id=""), {}),
        (marker(grant_id=7), {}),
        (marker(grant_id=None), {}),
        (marker(operation="regrant"), {}),
        (marker(operation=None), {}),
        (marker(operation=["grant"]), {}),
        ({}, {"run_metadata": marker()}),
        (marker(), {"run_metadata": {"temporary_access_grant_id": "grant-other"}}),
        (marker(), {"run_metadata": {"temporary_access_operation": "revoke"}}),
        (marker(), {"run_metadata": {"temporary_access_operation": None}}),
        (marker(), {"run_metadata": None}),
    ],
    ids=[
        "empty_grant",
        "integer_grant",
        "null_grant",
        "unknown_operation",
        "null_operation",
        "list_operation",
        "added",
        "rebound_grant",
        "changed_operation",
        "nulled_operation",
        "cleared",
    ],
)
async def test_an_operation_success_without_one_valid_persisted_marker_publishes_nothing(
    journal, redis, stored, update
):
    session = RunSession(journal, make_run(story_id=None, metadata=stored))

    run = await patch_run(session, redis, status=RunStatus.COMPLETED, result=SUCCESS, **update)

    assert run.result == SUCCESS
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields"),
    [
        (make_run(story_id=None), {"status": RunStatus.COMPLETED, "result": SUCCESS}),
        (make_run(), {"status": RunStatus.FAILED, "result": CODE_FIX}),
        (make_run(), {"status": RunStatus.RUNNING, "result": SUCCESS}),
        (make_run(), {"status": RunStatus.CANCELLED}),
        (make_run(), {"status": RunStatus.COMPLETED, "result": {"deploy_outcome": "deployed"}}),
    ],
    ids=["storyless", "code_fix", "intermediate", "no_result", "malformed_result"],
)
async def test_a_write_without_story_deploy_success_publishes_nothing(journal, redis, run, fields):
    await patch_run(RunSession(journal, run), redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields"),
    [
        (operation_run(), {"status": RunStatus.COMPLETED, "result": SKIPPED}),
        (operation_run(), {"status": RunStatus.COMPLETED}),
        (operation_run(), {"status": RunStatus.RUNNING, "result": SUCCESS}),
        (operation_run(), {"status": RunStatus.FAILED, "result": SUCCESS}),
        (operation_run(), {"status": RunStatus.CANCELLED, "result": SUCCESS}),
        (operation_run(), {"status": RunStatus.FAILED, "result": CODE_FIX}),
        (operation_run(), {"status": RunStatus.COMPLETED, "result": {"deploy_outcome": "ok"}}),
    ],
    ids=[
        "skipped",
        "no_result",
        "intermediate",
        "failed_success",
        "cancelled_success",
        "code_fix",
        "malformed_result",
    ],
)
async def test_a_write_without_operation_proof_publishes_nothing(journal, redis, run, fields):
    await patch_run(RunSession(journal, run), redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
async def test_a_malformed_stored_result_still_takes_an_unrelated_update(journal, redis):
    stored = {"deploy_outcome": "deployed"}
    session = RunSession(journal, make_run(status=RunStatus.COMPLETED.value, result=stored))

    run = await patch_run(session, redis, run_metadata={"deployed_by": "deployer"})

    assert run.result == stored
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields", "status_code"),
    [
        (make_run(), {"internal": False, "status": RunStatus.COMPLETED, "result": SUCCESS}, 401),
        (
            make_run(status=RunStatus.FAILED.value, result=CODE_FIX),
            {"status": RunStatus.COMPLETED, "result": SUCCESS},
            409,
        ),
        (
            make_run(status=RunStatus.COMPLETED.value, result=SUCCESS),
            {"status": RunStatus.RUNNING},
            409,
        ),
        (
            operation_run(),
            {"internal": False, "status": RunStatus.COMPLETED, "result": SUCCESS},
            401,
        ),
        (
            operation_run(status=RunStatus.FAILED.value, result=CODE_FIX),
            {"status": RunStatus.COMPLETED, "result": SUCCESS},
            409,
        ),
    ],
    ids=[
        "unauthenticated",
        "outcome_rewrite",
        "resurrection",
        "operation_unauthenticated",
        "operation_outcome_rewrite",
    ],
)
async def test_a_refused_update_publishes_nothing(journal, redis, run, fields, status_code):
    with pytest.raises(HTTPException) as refused:
        await patch_run(RunSession(journal, run), redis, **fields)

    assert refused.value.status_code == status_code
    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
async def test_zero_subscribers_is_a_published_hint(journal, redis, native, run, target):
    native.receivers = 0

    with capture_logs() as logs:
        await succeed(RunSession(journal, run()), redis)

    assert journal == woken(target)
    [published] = [log for log in logs if log["event"] == f"{target.value}_wake_published"]
    assert published["receivers"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, run, target, failure):
    native.failure = failure
    session = RunSession(journal, run())

    with capture_logs() as logs:
        updated = await succeed(session, redis)

    assert updated.status == RunStatus.COMPLETED
    assert updated.result == SUCCESS
    assert journal == COMMITTED
    [failed] = [log for log in logs if log["event"] == f"{target.value}_wake_failed"]
    assert failed == {
        "event": f"{target.value}_wake_failed",
        "log_level": "warning",
        "run_id": RUN_ID,
        "story_id": session.run.story_id,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "target"), PROOFS)
async def test_cancellation_during_publish_propagates_after_the_commit(
    journal, redis, native, run, target
):
    native.hold = asyncio.Event()

    request = asyncio.create_task(succeed(RunSession(journal, run()), redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit",)]
