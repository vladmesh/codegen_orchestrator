"""Offline contract tests for bounded settings-seed follow-up policy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pipeline_helpers
import pytest
import settings_seed_followup

from shared.contracts.queues.deploy import DeployOutcome

pytestmark = pytest.mark.needs_no_api_credential


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_preserves_explicit_zero_budgets(
    monkeypatch,
):
    """Each zero-valued test seam is distinct from the production defaults."""
    observed: dict[str, float] = {}

    async def follow(*_args, **kwargs):
        observed["repair_budget"] = kwargs["repair_budget"]
        observed["retry_budget"] = kwargs["retry_budget"]
        observed["overall_budget"] = kwargs["overall_budget"]

    monkeypatch.setattr(pipeline_helpers, "follow_settings_seed", follow)

    await pipeline_helpers.wait_settings_seed_followup(
        SimpleNamespace(), {}, SimpleNamespace(), repair_budget=0, retry_budget=0, overall_budget=0
    )

    assert observed == {"repair_budget": 0, "retry_budget": 0, "overall_budget": 0}


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_can_limit_manifest_repairs_for_a_brief(monkeypatch):
    observed: dict[str, int | None] = {}

    async def follow(*_args, **kwargs):
        observed["max_manifest_repairs"] = kwargs["max_manifest_repairs"]

    monkeypatch.setattr(pipeline_helpers, "follow_settings_seed", follow)

    await pipeline_helpers.wait_settings_seed_followup(
        SimpleNamespace(), {}, SimpleNamespace(), max_manifest_repairs=1
    )

    assert observed == {"max_manifest_repairs": 1}


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_default_budgets_cover_the_full_followup_deploy(
    monkeypatch,
):
    """The follow-up outcome wait includes deploy.yml, unlike the first pass."""
    observed: dict[str, float] = {}

    async def follow(*_args, **kwargs):
        observed["repair_budget"] = kwargs["repair_budget"]
        observed["retry_budget"] = kwargs["retry_budget"]
        observed["overall_budget"] = kwargs["overall_budget"]

    monkeypatch.setattr(pipeline_helpers, "follow_settings_seed", follow)

    await pipeline_helpers.wait_settings_seed_followup(SimpleNamespace(), {}, SimpleNamespace())

    assert observed == {
        "repair_budget": (
            pipeline_helpers.LLM_ENGINEERING_TIMEOUT
            + pipeline_helpers.DEPLOY_RUN_TIMEOUT
            + pipeline_helpers.DEPLOY_TIMEOUT
            + pipeline_helpers.DEPLOY_OUTCOME_TIMEOUT
        ),
        "retry_budget": (
            pipeline_helpers.DEPLOY_RUN_TIMEOUT
            + pipeline_helpers.DEPLOY_TIMEOUT
            + pipeline_helpers.DEPLOY_OUTCOME_TIMEOUT
        ),
        "overall_budget": pipeline_helpers.SETTINGS_SEED_FOLLOWUP_TIMEOUT,
    }


@pytest.mark.asyncio
async def test_follow_settings_seed_refuses_before_an_overall_deadline_is_spent():
    """The shipped cap is an actual lifecycle bound, not runner-only arithmetic."""
    api = SimpleNamespace(get=AsyncMock())
    ctx = {"story_id": "story-1", "deploy_run_id": "deploy-old"}
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    result = await settings_seed_followup.follow_settings_seed(
        api,
        ctx,
        failed,
        repair_budget=1,
        retry_budget=1,
        overall_budget=0,
        poll_interval=0,
        on_poll=None,
        wait_followup=AsyncMock(),
    )

    assert result is None
    assert ctx["settings_seed_repair_error"] == (
        "settings-seed follow-up exhausted its overall lifecycle deadline"
    )
    api.get.assert_not_awaited()


def _deploy_run(
    run_id: str,
    *,
    story_id: str = "story-1",
    head_sha: str | None = "abc123",
    user_id: int | None = None,
    created_at: str = "2026-09-04T00:00:00Z",
) -> dict:
    metadata = {"triggered_by": "pr_poll", "head_sha": head_sha} if head_sha else {}
    return {
        "id": run_id,
        "type": "deploy",
        "project_id": "project-1",
        "story_id": story_id,
        "user_id": user_id,
        "status": "completed",
        "created_at": created_at,
        "run_metadata": metadata,
    }


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_reaches_the_next_successful_deploy(monkeypatch):
    """A repairable failed seed is lifecycle progress, not the mega's verdict."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = {
        **_deploy_run("deploy-poll-old", head_sha="abc123"),
        "status": "failed",
        "result": {
            "deploy_outcome": "settings_seed_failed",
            "deploy_fix_attempt": 0,
            "error_details": "settings_seed:key_not_declared",
            "settings_seed": [
                {
                    "key": "languages",
                    "scope": "product",
                    "subject_id": None,
                    "written": False,
                    "failure": "key_not_declared",
                }
            ],
        },
    }
    repair = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "type": "engineering",
        "project_id": "project-1",
        "story_id": "story-1",
        "task_id": None,
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
        "result": {"engineering_status": "completed"},
    }
    final = {
        **_deploy_run("deploy-poll-repaired", head_sha="def456", created_at="2026-09-04T00:01:00Z"),
        "status": "completed",
        "result": {
            "deploy_outcome": "success",
            "deploy_fix_attempt": 1,
            "settings_seed": [
                {
                    "key": "languages",
                    "scope": "product",
                    "subject_id": None,
                    "written": True,
                    "failure": None,
                }
            ],
        },
    }

    deploy_list_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deploy_list_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == "/api/runs/":
            if request.url.params["run_type"] == "engineering":
                return httpx.Response(200, json=[repair])
            deploy_list_reads += 1
            if deploy_list_reads == 1:
                return httpx.Response(200, json=[initial])
            return httpx.Response(200, json=[final, initial])
        if request.url.path == "/api/runs/deploy-poll-old":
            return httpx.Response(200, json=initial)
        if request.url.path == "/api/runs/eng-deploy-fix-deploy-poll-old-1":
            return httpx.Response(200, json=repair)
        if request.url.path == "/api/runs/deploy-poll-repaired":
            return httpx.Response(200, json=final)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {"project_id": "project-1", "story_id": "story-1", "deploy_run_id": initial["id"]}
    polls: list[None] = []
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        failed = await pipeline_helpers.wait_deploy_outcome(
            api_internal, ctx, timeout=1, poll_interval=0
        )
        repaired = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
            on_poll=lambda: polls.append(None),
        )

    assert repaired is not None and repaired.deploy_outcome is DeployOutcome.SUCCESS
    assert ctx["deploy_run_id"] == final["id"]
    assert ctx["deploy_outcome"] == DeployOutcome.SUCCESS.value
    current = ctx["deploy_run_record"]["current"]
    assert current["id"] == final["id"]
    prior = ctx["deploy_run_record"]["prior_attempts"]
    assert [record["id"] for record in prior] == [initial["id"]]
    assert prior[0]["deploy_outcome"] == DeployOutcome.SETTINGS_SEED_FAILED.value
    assert prior[0]["settings_seed"] == initial["result"]["settings_seed"]
    assert polls


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_binds_a_second_repair_to_its_current_deploy(monkeypatch):
    """A retry between repairs cannot rediscover the first repair by attempt."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    def failed(run_id: str, created_at: str, failure: str) -> dict:
        return {
            **_deploy_run(run_id, created_at=created_at),
            "status": "failed",
            "result": {
                "deploy_outcome": "settings_seed_failed",
                "deploy_fix_attempt": 0,
                "settings_seed": [
                    {
                        "key": "languages",
                        "scope": "product",
                        "written": False,
                        "failure": failure,
                    }
                ],
            },
        }

    initial = failed("deploy-poll-initial", "2026-09-04T00:00:00Z", "key_not_declared")
    convergent = failed("deploy-poll-retry", "2026-09-04T00:01:00Z", "transport")
    repaired_seed = failed("deploy-poll-second", "2026-09-04T00:02:00Z", "key_not_declared")
    final = {
        **_deploy_run("deploy-poll-final", created_at="2026-09-04T00:03:00Z"),
        "result": {"deploy_outcome": "success"},
    }
    repair_one = {
        "id": "eng-deploy-fix-deploy-poll-initial-1",
        "type": "engineering",
        "story_id": "story-1",
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    repair_two_running = {
        "id": "eng-deploy-fix-deploy-poll-second-1",
        "type": "engineering",
        "story_id": "story-1",
        "status": "running",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    repair_two_done = {**repair_two_running, "status": "completed"}
    deploy_reads = 0
    repair_two_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deploy_reads, repair_two_reads
        path = request.url.path
        if path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if path == "/api/system-configs/deploy.max_deploy_retries":
            return httpx.Response(200, json={"value": 2})
        if path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if path == "/api/runs/":
            assert request.url.params["run_type"] == "deploy"
            deploy_reads += 1
            return httpx.Response(
                200,
                json=(
                    [convergent, initial]
                    if deploy_reads == 1
                    else [repaired_seed, convergent, initial]
                    if deploy_reads == 2
                    else [final, repaired_seed, convergent, initial]
                ),
            )
        if path == "/api/runs/eng-deploy-fix-deploy-poll-initial-1":
            return httpx.Response(200, json=repair_one)
        if path == "/api/runs/eng-deploy-fix-deploy-poll-second-1":
            repair_two_reads += 1
            return httpx.Response(
                200, json=repair_two_running if repair_two_reads == 1 else repair_two_done
            )
        if path == "/api/runs/eng-deploy-fix-deploy-poll-retry-1":
            raise AssertionError("a convergent deploy must not bind an earlier repair")
        if path == "/api/runs/deploy-poll-retry":
            return httpx.Response(200, json=convergent)
        if path == "/api/runs/deploy-poll-second":
            return httpx.Response(200, json=repaired_seed)
        if path == "/api/runs/deploy-poll-final":
            return httpx.Response(200, json=final)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    initial_result = pipeline_helpers.DeployRunResult(**initial["result"])
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            initial_result,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is not None and result.deploy_outcome is DeployOutcome.SUCCESS
    assert ctx["settings_seed_repair_run_ids"] == [repair_one["id"], repair_two_done["id"]]
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair_one["id"], "status": "completed", "error": None},
        {"attempt": 1, "run_id": repair_two_done["id"], "status": "completed", "error": None},
    ]
    assert repair_two_reads == 2


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_ignores_older_runs_until_a_fresh_deploy(
    monkeypatch,
):
    """A previous deploy cannot satisfy the repair's later-deploy wait."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = {
        **_deploy_run("deploy-poll-current", head_sha="abc123"),
        "status": "failed",
        "result": {
            "deploy_outcome": "settings_seed_failed",
            "settings_seed": [
                {
                    "key": "languages",
                    "scope": "product",
                    "written": False,
                    "failure": "key_not_declared",
                }
            ],
        },
    }
    stale = {
        **_deploy_run("deploy-poll-stale", head_sha="old456"),
        "status": "completed",
        "result": {"deploy_outcome": "success"},
    }
    repair = {
        "id": "eng-deploy-fix-deploy-poll-current-1",
        "type": "engineering",
        "story_id": "story-1",
        "task_id": None,
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    fresh = {
        **_deploy_run("deploy-poll-fresh", head_sha="fresh789", created_at="2026-09-04T00:02:00Z"),
        "status": "completed",
        "result": {"deploy_outcome": "success", "deploy_fix_attempt": 1},
    }
    deploy_list_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deploy_list_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == "/api/runs/":
            if request.url.params["run_type"] == "engineering":
                return httpx.Response(200, json=[repair])
            deploy_list_reads += 1
            if deploy_list_reads < 3:
                return httpx.Response(200, json=[stale, initial])
            return httpx.Response(200, json=[fresh, stale, initial])
        if request.url.path == "/api/runs/eng-deploy-fix-deploy-poll-current-1":
            return httpx.Response(200, json=repair)
        if request.url.path == "/api/runs/deploy-poll-fresh":
            return httpx.Response(200, json=fresh)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "project_id": "project-1",
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    failed = pipeline_helpers.DeployRunResult(**initial["result"])
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is not None and result.deploy_outcome is DeployOutcome.SUCCESS
    assert ctx["deploy_run_id"] == fresh["id"]
    assert deploy_list_reads >= 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [("transport",), ("key_not_declared", "transport")])
async def test_wait_settings_seed_followup_follows_a_convergent_same_commit_retry(
    monkeypatch, failures
):
    """A scheduler retry is progress too, even though it has no repair Run."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = _deploy_run("deploy-poll-current", head_sha="abc123")
    fresh = {
        **_deploy_run("deploy-poll-retry", head_sha="abc123", created_at="2026-09-04T00:01:00Z"),
        "status": "completed",
        "result": {"deploy_outcome": "success"},
    }
    deploy_list_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deploy_list_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_retries":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == "/api/runs/":
            deploy_list_reads += 1
            return httpx.Response(
                200, json=[initial] if deploy_list_reads == 1 else [fresh, initial]
            )
        if request.url.path == "/api/runs/deploy-poll-retry":
            return httpx.Response(200, json=fresh)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": f"setting-{index}",
                "scope": "product",
                "written": False,
                "failure": failure,
            }
            for index, failure in enumerate(failures)
        ],
    )
    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is not None and result.deploy_outcome is DeployOutcome.SUCCESS
    assert ctx["deploy_run_id"] == fresh["id"]


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_stops_at_the_scheduler_retry_cap(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = _deploy_run("deploy-poll-current", created_at="2026-09-04T00:00:00Z")
    retry = {
        **_deploy_run("deploy-poll-retry", created_at="2026-09-04T00:01:00Z"),
        "result": {
            "deploy_outcome": "settings_seed_failed",
            "settings_seed": [
                {"key": "languages", "scope": "product", "written": False, "failure": "transport"}
            ],
        },
    }

    retry_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal retry_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_retries":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == "/api/runs/":
            return httpx.Response(200, json=[retry, initial])
        if request.url.path == "/api/runs/deploy-poll-retry":
            retry_reads += 1
            return httpx.Response(200, json=retry)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {"key": "languages", "scope": "product", "written": False, "failure": "transport"}
        ],
    )
    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is None
    assert ctx["settings_seed_repair_error"] == "settings-seed retry exceeded scheduler cap 2"
    assert retry_reads == 1


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_stops_a_convergent_retry_when_story_failed(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {"key": "languages", "scope": "product", "written": False, "failure": "transport"}
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system-configs/deploy.max_deploy_retries":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "failed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-current",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is None
    assert "story story-1 reached failed" in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_stops_at_the_scheduler_repair_cap(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        deploy_fix_attempt=2,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts"
        return httpx.Response(200, json={"value": 2})

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await pipeline_helpers.wait_settings_seed_followup(client, ctx, failed)

    assert result is None
    assert "scheduler repair cap 2" in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_applies_a_brief_repair_ceiling_before_a_second_repair(
    monkeypatch,
):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        deploy_fix_attempt=1,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts"
        return httpx.Response(200, json={"value": 2})

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await pipeline_helpers.wait_settings_seed_followup(
            client, ctx, failed, max_manifest_repairs=1
        )

    assert result is None
    assert "brief harness repair ceiling 1" in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_fragment"),
    [
        (httpx.Response(404), "HTTPStatusError"),
        (httpx.Response(200, json={"value": "two"}), "must be a positive integer"),
    ],
)
async def test_wait_settings_seed_followup_records_an_unread_scheduler_cap(
    monkeypatch, response, error_fragment
):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts"
        return response

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await pipeline_helpers.wait_settings_seed_followup(client, ctx, failed)

    assert result is None
    assert "deploy.max_deploy_fix_attempts" in ctx["settings_seed_repair_error"]
    assert error_fragment in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_records_an_invalid_source_timestamp(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts"
        return httpx.Response(200, json={"value": 2})

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "not-a-timestamp",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await pipeline_helpers.wait_settings_seed_followup(client, ctx, failed)

    assert result is None
    assert "source deploy timestamp is invalid" in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
async def test_manifest_repair_discovery_timeout_is_retained_in_context():
    ctx = {"story_id": "story-1"}
    client = SimpleNamespace(get=AsyncMock())
    story_alive = AsyncMock(return_value=True)

    repair = await settings_seed_followup._wait_for_manifest_repair_run(
        client,
        ctx,
        source_run_id="deploy-poll-current",
        attempt=1,
        deadline=0,
        poll_interval=0,
        on_poll=None,
        story_alive=story_alive,
    )

    assert repair is None
    assert ctx["settings_seed_repair_error"] == (
        "no manifest repair attempt 1 appeared for story story-1 before the repair deadline"
    )
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_manifest_repair_wait_obeys_its_attempt_deadline():
    client = SimpleNamespace(get=AsyncMock())
    story_alive = AsyncMock(return_value=True)

    repair = await settings_seed_followup._wait_for_terminal_run(
        client,
        {"id": "eng-deploy-fix-deploy-poll-current-1", "status": "running"},
        deadline=0,
        poll_interval=0,
        on_poll=None,
        story_alive=story_alive,
    )

    assert repair is None
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_stops_on_terminal_failure_without_polling():
    terminal = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "value_rejected",
            }
        ],
    )
    client = SimpleNamespace(get=AsyncMock())

    result = await pipeline_helpers.wait_settings_seed_followup(
        client,
        {"deploy_run_id": "deploy-terminal", "story_id": "story-1"},
        terminal,
        repair_budget=1,
        retry_budget=1,
        overall_budget=1,
    )

    assert result is terminal
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_settings_seed_followup_stops_when_the_story_is_failed(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed_seed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/runs/":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "failed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "deploy_run_id": "deploy-exhausted",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
        "story_id": "story-1",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api_internal:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api_internal,
            ctx,
            failed_seed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is None
    assert "reached failed before manifest repair attempt 1" in ctx["settings_seed_repair_error"]


@pytest.mark.asyncio
async def test_already_failed_manifest_repair_is_retained_before_its_terminal_reason(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    failed = pipeline_helpers.DeployRunResult(
        deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
        settings_seed=[
            {
                "key": "languages",
                "scope": "product",
                "written": False,
                "failure": "key_not_declared",
            }
        ],
    )
    repair = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "failed",
        "run_metadata": {"deploy_fix_attempt": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == "/api/runs/eng-deploy-fix-deploy-poll-old-1":
            return httpx.Response(200, json=repair)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api, ctx, failed, repair_budget=1, retry_budget=1, overall_budget=1, poll_interval=0
        )

    assert result is None
    assert ctx["settings_seed_repair_run_ids"] == [repair["id"]]
    assert ctx["settings_seed_repair_error"] == f"manifest repair Run {repair['id']} ended failed"
    assert ctx["settings_seed_repair_attempts"] == [
        {
            "attempt": 1,
            "run_id": repair["id"],
            "status": "failed",
            "error": f"manifest repair Run {repair['id']} ended failed",
        }
    ]
