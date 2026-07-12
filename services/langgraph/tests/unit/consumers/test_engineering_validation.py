"""Engineering consumer validates its input via EngineeringMessage before business logic."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.queues.engineering import EngineeringMessage
from src.consumers.engineering import process_engineering_job


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "http://api/runs/eng-1")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_malformed_job_is_terminal_not_poison_loop():
    """A job missing a required field fails the run and returns (so the queue loop ACKs
    it) instead of raising — a raise would leave the entry unacked and reclaim forever."""
    api = AsyncMock()
    redis = AsyncMock()
    # project_id is required by EngineeringMessage — omit it.
    bad_job = {"task_id": "eng-1", "user_id": "123", "action": "feature"}

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.consumers.engineering_result_handler.api_client", api),
    ):
        result = await process_engineering_job(bad_job, redis)

    # Terminal: returns a result (loop ACKs), never reaches business logic.
    assert result["status"] == "failed"
    api.get_project.assert_not_called()
    # The identifiable run is failed with a visible terminal outcome.
    failed = [c for c in api.patch.call_args_list if c.args and c.args[0] == "runs/eng-1"]
    assert failed, "expected the run to be failed"
    assert failed[0].kwargs["json"]["status"] == "failed"


@pytest.mark.asyncio
async def test_malformed_job_reraises_when_terminal_write_is_transiently_lost():
    """If failing the run hits a transient API error (5xx), do NOT ACK — re-raise so the
    queue loop leaves the poison entry for claim_pending to retry once the API recovers."""
    api = AsyncMock()
    api.patch = AsyncMock(side_effect=_http_error(503))
    redis = AsyncMock()
    bad_job = {"task_id": "eng-1", "user_id": "123", "action": "feature"}  # missing project_id

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.consumers.engineering_result_handler.api_client", api),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await process_engineering_job(bad_job, redis)

    api.get_project.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_job_acks_when_run_is_non_retryably_unwritable():
    """A non-retryable client error (404 — no such run) is terminal: return so the loop ACKs
    instead of poison-looping on a run that will never accept the write."""
    api = AsyncMock()
    api.patch = AsyncMock(side_effect=_http_error(404))
    redis = AsyncMock()
    bad_job = {"task_id": "eng-1", "user_id": "123", "action": "feature"}

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.consumers.engineering_result_handler.api_client", api),
    ):
        result = await process_engineering_job(bad_job, redis)

    assert result["status"] == "failed"
    api.get_project.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_job_without_task_id_still_terminates():
    """No identifiable run — still returns terminally and touches no business logic."""
    api = AsyncMock()
    redis = AsyncMock()
    bad_job = {"user_id": "123", "action": "feature"}  # no task_id, no project_id

    with patch("src.consumers.engineering.api_client", api):
        result = await process_engineering_job(bad_job, redis)

    assert result["status"] == "failed"
    api.patch.assert_not_called()
    api.get_project.assert_not_called()


def test_unknown_extra_fields_are_ignored_and_defaults_apply():
    """Wire additions do not crash the boundary; omitted optional fields take defaults."""
    msg = EngineeringMessage.model_validate(
        {"task_id": "t", "project_id": "p", "user_id": "u", "surprise": "x"}
    )
    assert msg.task_id == "t"
    assert msg.action.value == "create"
    assert msg.deploy_fix_attempt == 0
    assert msg.skip_deploy is False
