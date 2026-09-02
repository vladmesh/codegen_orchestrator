"""The narrow client for the generated product's released core jobs contract."""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.clients.product_jobs import (
    GeneratedServiceJobsClient,
    JobCallFailure,
)

_FIRE = httpx.Request("POST", "https://service/jobs/fire")
_EVIDENCE = httpx.Request("POST", "https://service/jobs/evidence")


def _command(dispatch_status="dispatched", **overrides) -> dict:
    return {
        "contract_version": 1,
        "command_id": "qa-run-7-daily_digest",
        "name": "daily_digest",
        "arguments": {},
        "fired_by_product": "project-1",
        "fired_by_run": "run-7",
        "dispatch_status": dispatch_status,
        "accepted_at": "2026-09-02T10:00:00Z",
        "dispatched_at": "2026-09-02T10:00:01Z",
        **overrides,
    }


async def _fire(transport, **overrides):
    return await GeneratedServiceJobsClient("https://service", transport=transport).fire(
        **{
            "command_id": "qa-run-7-daily_digest",
            "name": "daily_digest",
            "arguments": {"chat_id": 42},
            "fired_by_product": "project-1",
            "fired_by_run": "run-7",
            "capability": "never-in-a-url",
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_a_fire_is_the_released_route_with_the_capability_in_one_header():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(200, json=_command(), request=_FIRE)

    outcome = await _fire(transport)

    method, url = transport.request.call_args.args
    kwargs = transport.request.call_args.kwargs
    assert (method, url) == ("POST", "https://service/jobs/fire")
    assert kwargs["json"] == {
        "contract_version": 1,
        "command_id": "qa-run-7-daily_digest",
        "name": "daily_digest",
        "arguments": {"chat_id": 42},
        "fired_by_product": "project-1",
        "fired_by_run": "run-7",
    }
    # Exactly one header, and the capability is in it and nowhere else.
    assert kwargs["headers"] == {"X-Jobs-Capability": "never-in-a-url"}
    assert "never-in-a-url" not in url
    assert "never-in-a-url" not in str(kwargs["json"])
    assert outcome.command is not None
    assert outcome.command.dispatch_status == "dispatched"


@pytest.mark.asyncio
async def test_reading_evidence_back_carries_no_capability():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(200, json=_command(), request=_EVIDENCE)

    outcome = await GeneratedServiceJobsClient("https://service", transport=transport).evidence(
        command_id="qa-run-7-daily_digest", fired_by_product="project-1"
    )

    method, url = transport.request.call_args.args
    kwargs = transport.request.call_args.kwargs
    assert (method, url) == ("POST", "https://service/jobs/evidence")
    assert kwargs["headers"] is None
    assert kwargs["json"] == {
        "contract_version": 1,
        "command_id": "qa-run-7-daily_digest",
        "fired_by_product": "project-1",
    }
    assert outcome.command is not None


@pytest.mark.asyncio
async def test_an_undeclared_name_is_the_products_own_refusal():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(
        404, json={"detail": "Job name not declared"}, request=_FIRE
    )

    outcome = await _fire(transport)

    assert outcome.command is None
    assert outcome.failure is JobCallFailure.NAME_NOT_DECLARED


@pytest.mark.asyncio
async def test_arguments_the_declared_schema_refuses_are_their_own_outcome():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(
        422, json={"detail": "Job arguments do not satisfy the declared schema"}, request=_FIRE
    )

    outcome = await _fire(transport)

    assert outcome.failure is JobCallFailure.ARGUMENTS_REJECTED


@pytest.mark.asyncio
async def test_a_404_on_evidence_means_no_command_was_recorded_not_an_undeclared_name():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(404, json={}, request=_EVIDENCE)

    outcome = await GeneratedServiceJobsClient("https://service", transport=transport).evidence(
        command_id="qa-run-7-daily_digest", fired_by_product="project-1"
    )

    assert outcome.failure is JobCallFailure.NO_COMMAND_RECORDED


@pytest.mark.asyncio
async def test_an_unreachable_product_is_a_transport_outcome_not_an_exception():
    transport = AsyncMock()
    transport.request.side_effect = httpx.ConnectError("no route")

    outcome = await _fire(transport)

    assert outcome.command is None
    assert outcome.failure is JobCallFailure.TRANSPORT


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_job_command_proves_nothing():
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(200, json={"ok": True}, request=_FIRE)

    outcome = await _fire(transport)

    assert outcome.failure is JobCallFailure.MALFORMED_ANSWER


@pytest.mark.asyncio
async def test_an_undelivered_command_is_reported_as_the_product_recorded_it():
    """`undelivered` is a real state of the contract, not a client failure."""
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(
        200, json=_command(dispatch_status="undelivered", dispatched_at=None), request=_FIRE
    )

    outcome = await _fire(transport)

    assert outcome.failure is None
    assert outcome.command is not None
    assert outcome.command.dispatch_status == "undelivered"
    assert outcome.command.dispatched_at is None
