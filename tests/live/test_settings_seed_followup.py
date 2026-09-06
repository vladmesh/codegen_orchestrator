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
        # The Run source is consulted before the story on every pass, so a story
        # refusal is now preceded by the deploy-Run read that found nothing.
        if request.url.path == "/api/runs/":
            return httpx.Response(200, json=[])
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


def _run_reader(payload: dict, status_code: int = 200):
    """A minimal client that answers every Run read with one payload."""
    reads: list[str] = []

    async def get(url: str):
        reads.append(url)
        return httpx.Response(status_code, json=payload, request=httpx.Request("GET", url))

    return SimpleNamespace(get=get), reads


@pytest.mark.asyncio
async def test_manifest_repair_discovery_timeout_takes_one_final_read_first():
    """An expired budget names a timeout only after a read found nothing.

    This replaces the previous assertion that an expired deadline performed no
    read at all. That is the defect the card closes: a repair Run the scheduler
    created while the wait slept was reported as a deadline exit although it was
    already there to be read.
    """
    ctx = {"story_id": "story-1"}
    client, reads = _run_reader({}, status_code=404)
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
    assert reads == ["/api/runs/eng-deploy-fix-deploy-poll-current-1"]


@pytest.mark.asyncio
async def test_a_repair_run_that_settled_at_the_deadline_is_read_not_timed_out():
    """The typed terminal fact wins over the clock that expired beside it."""
    terminal = {
        "id": "eng-deploy-fix-deploy-poll-current-1",
        "status": "failed",
        "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
    }
    client, reads = _run_reader(terminal)

    repair = await settings_seed_followup._wait_for_terminal_run(
        client,
        {},
        {"id": terminal["id"], "status": "running"},
        deadline=0,
        poll_interval=0,
        on_poll=None,
        story_alive=AsyncMock(return_value=True),
    )

    assert repair == terminal
    assert reads == [f"/api/runs/{terminal['id']}"]


@pytest.mark.asyncio
async def test_terminal_manifest_repair_wait_obeys_its_attempt_deadline():
    """A Run still not terminal on that final read is a genuine deadline exit."""
    running = {"id": "eng-deploy-fix-deploy-poll-current-1", "status": "running"}
    client, reads = _run_reader(running)

    repair = await settings_seed_followup._wait_for_terminal_run(
        client,
        {},
        running,
        deadline=0,
        poll_interval=0,
        on_poll=None,
        story_alive=AsyncMock(return_value=True),
    )

    assert repair is None
    assert reads == [f"/api/runs/{running['id']}"]


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
        # No repair Run yet; the story source answers on the same pass.
        if request.url.path == "/api/runs/eng-deploy-fix-deploy-exhausted-1":
            return httpx.Response(404)
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


@pytest.mark.asyncio
async def test_a_repair_without_a_new_commit_stops_and_names_that_cause(monkeypatch):
    """`no_new_commit` is the reason, not the terminal status every failure shares."""
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
        "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
    }
    story_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal story_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            story_reads += 1
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == f"/api/runs/{repair['id']}":
            return httpx.Response(200, json=repair)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    polls: list[None] = []
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
            on_poll=lambda: polls.append(None),
        )

    expected = f"manifest repair Run {repair['id']} ended failed: no_new_commit"
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair["id"], "status": "failed", "error": expected}
    ]
    # No sleep between the two passes, and no story read at all: the Run source
    # answered on both, and the resolver's precedence means the cause is named
    # without consulting the story parking that is its consequence.
    assert len(polls) == 2
    assert story_reads == 0


@pytest.mark.asyncio
async def test_a_followup_deploy_skipped_as_already_deployed_stops_the_wait(monkeypatch):
    """A skipped deploy seeded nothing, so no later poll can make it a success."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = {
        **_deploy_run("deploy-poll-old"),
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
    repair = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
        "result": {"engineering_status": "completed"},
    }
    skipped = {
        **_deploy_run("deploy-poll-skipped", created_at="2026-09-04T00:01:00Z"),
        "status": "completed",
        "result": {
            "deploy_outcome": "success",
            "deploy_fix_attempt": 1,
            "application_id": 17,
            "skipped_reason": "already_deployed_same_sha",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if path == "/api/runs/":
            return httpx.Response(200, json=[skipped, initial])
        if path == f"/api/runs/{repair['id']}":
            return httpx.Response(200, json=repair)
        if path == "/api/runs/deploy-poll-skipped":
            return httpx.Response(200, json=skipped)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api,
            ctx,
            pipeline_helpers.DeployRunResult(**initial["result"]),
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    expected = (
        "settings-seed follow-up deploy run deploy-poll-skipped performed no "
        "deployment: already_deployed_same_sha"
    )
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    # The reason reaches the repair-attempt record too, so a red artifact says
    # which repair the skip ended.
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair["id"], "status": "completed", "error": expected}
    ]


@pytest.mark.asyncio
async def test_a_story_already_parked_refuses_before_a_repair_is_awaited(monkeypatch):
    """The story a no-new-commit repair parks is a terminal refusal, not a wait."""
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
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        # No repair Run exists; the Run source is still read first on the pass.
        if request.url.path == "/api/runs/eng-deploy-fix-deploy-poll-old-1":
            return httpx.Response(404)
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "waiting_human_review"})
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
            api,
            ctx,
            failed,
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is None
    assert ctx["settings_seed_repair_story_status"] == "waiting_human_review"
    assert ctx["settings_seed_repair_error"] == (
        "story story-1 reached waiting_human_review before manifest repair attempt 1"
    )


@pytest.mark.asyncio
async def test_a_healthy_repair_with_a_real_followup_deploy_is_still_awaited(monkeypatch):
    """None of the new exits may cut short a deploy that actually deployed."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = {
        **_deploy_run("deploy-poll-old"),
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
    repair_running = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "running",
        "run_metadata": {"deploy_fix_attempt": 1},
        "result": None,
    }
    repair_done = {
        **repair_running,
        "status": "completed",
        "result": {"engineering_status": "completed"},
    }
    fresh_queued = {
        **_deploy_run("deploy-poll-fresh", created_at="2026-09-04T00:01:00Z"),
        "status": "running",
        "result": None,
    }
    fresh_done = {
        **fresh_queued,
        "status": "completed",
        "result": {"deploy_outcome": "success", "deploy_fix_attempt": 1},
    }
    repair_reads = 0
    fresh_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal repair_reads, fresh_reads
        path = request.url.path
        if path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if path == "/api/runs/":
            return httpx.Response(200, json=[fresh_queued, initial])
        if path == f"/api/runs/{repair_running['id']}":
            repair_reads += 1
            return httpx.Response(200, json=repair_running if repair_reads == 1 else repair_done)
        if path == "/api/runs/deploy-poll-fresh":
            fresh_reads += 1
            return httpx.Response(200, json=fresh_queued if fresh_reads == 1 else fresh_done)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api,
            ctx,
            pipeline_helpers.DeployRunResult(**initial["result"]),
            repair_budget=1,
            retry_budget=1,
            overall_budget=1,
            poll_interval=0,
        )

    assert result is not None and result.deploy_outcome is DeployOutcome.SUCCESS
    assert result.skipped_reason is None
    assert ctx["deploy_run_id"] == "deploy-poll-fresh"
    assert "settings_seed_repair_error" not in ctx
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair_done["id"], "status": "completed", "error": None}
    ]
    # Both the running repair and the running deploy were awaited to terminal.
    assert repair_reads == 2
    assert fresh_reads == 2


@pytest.mark.asyncio
async def test_a_followup_wait_that_names_no_reason_still_ends_named():
    """Every exit out of a manifest repair carries a reason, whatever the wait does."""
    repair = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    ctx = {
        "story_id": "story-1",
        "deploy_run_id": "deploy-poll-old",
        "deploy_run_created_at": "2026-09-04T00:00:00Z",
    }
    api = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: repair
            )
        )
    )
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

    result, _ = await settings_seed_followup._follow_manifest_repair(
        api,
        ctx,
        failed,
        repair_cap=2,
        repair_cap_label="scheduler repair cap",
        overall_deadline=settings_seed_followup.time.monotonic() + 5,
        repair_budget=5,
        poll_interval=0,
        on_poll=None,
        wait_followup=AsyncMock(return_value=None),
    )

    assert result is None
    assert ctx["settings_seed_repair_error"] == (
        "manifest repair attempt 1 follow-up deploy ended without a reason"
    )
    assert ctx["settings_seed_repair_attempts"][0]["error"] == (
        "manifest repair attempt 1 follow-up deploy ended without a reason"
    )


@pytest.mark.asyncio
async def test_a_story_parked_while_a_repair_runs_stops_the_wait_within_one_poll(monkeypatch):
    """The gate is never slower than the wait it gates, and its reason is the exit.

    The story is `in_progress` on the first read and `waiting_human_review` on
    the next, while the repair Run stays `running` throughout — the real race the
    gate exists for. With a story cache longer than the poll interval the refusal
    would arrive two or three polls late, and the attempt record would have said
    the repair timed out.
    """
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
        "status": "running",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    story_reads = 0
    repair_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal story_reads, repair_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            story_reads += 1
            status = "in_progress" if story_reads == 1 else "waiting_human_review"
            return httpx.Response(200, json={"status": status})
        if request.url.path == f"/api/runs/{repair['id']}":
            repair_reads += 1
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
            api,
            ctx,
            failed,
            repair_budget=60,
            retry_budget=60,
            overall_budget=60,
            poll_interval=0,
        )

    expected = "story story-1 reached waiting_human_review before manifest repair attempt 1"
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    # The attempt record says what the run-level evidence says. It used to say
    # "manifest repair attempt 1 timed out" for the very same event.
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair["id"], "status": "running", "error": expected}
    ]
    # Discovery answered from the Run source alone, so the story was not read
    # there. The terminal wait then read the Run and the story on each of its two
    # passes: `in_progress` on the first, the refusal on the second.
    assert story_reads == 2
    assert repair_reads == 3


@pytest.mark.asyncio
async def test_a_repair_that_fails_as_the_deadline_expires_names_no_new_commit(monkeypatch):
    """A repair Run settling at the deadline ends the wait on its reason, not the clock."""
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
    running = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "running",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    settled = {
        **running,
        "status": "failed",
        "result": {"engineering_status": "failed", "failure_reason": "no_new_commit"},
    }
    repair_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal repair_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path == f"/api/runs/{running['id']}":
            repair_reads += 1
            # Discovery sees it running; the Run settles while the budget expires.
            return httpx.Response(200, json=running if repair_reads == 1 else settled)
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
            api,
            ctx,
            failed,
            # An exhausted repair budget: the terminal wait is past its deadline
            # on its very first pass and must still take one read.
            repair_budget=0,
            retry_budget=60,
            overall_budget=60,
            poll_interval=0,
        )

    expected = f"manifest repair Run {running['id']} ended failed: no_new_commit"
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": running["id"], "status": "failed", "error": expected}
    ]
    assert repair_reads == 2


@pytest.mark.asyncio
async def test_a_followup_deploy_that_settles_at_the_deadline_names_the_skip(monkeypatch):
    """A skipped deploy readable as the budget expires is the exit, not a timeout."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    initial = {
        **_deploy_run("deploy-poll-old"),
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
    repair = {
        "id": "eng-deploy-fix-deploy-poll-old-1",
        "story_id": "story-1",
        "status": "completed",
        "run_metadata": {"deploy_fix_attempt": 1},
        "result": {"engineering_status": "completed"},
    }
    skipped = {
        **_deploy_run("deploy-poll-skipped", created_at="2026-09-04T00:01:00Z"),
        "status": "completed",
        "result": {
            "deploy_outcome": "success",
            "deploy_fix_attempt": 1,
            "application_id": 17,
            "skipped_reason": "already_deployed_same_sha",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "in_progress"})
        if path == "/api/runs/":
            return httpx.Response(200, json=[skipped, initial])
        if path == f"/api/runs/{repair['id']}":
            return httpx.Response(200, json=repair)
        if path == "/api/runs/deploy-poll-skipped":
            return httpx.Response(200, json=skipped)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ctx = {
        "story_id": "story-1",
        "deploy_run_id": initial["id"],
        "deploy_run_created_at": initial["created_at"],
    }
    async with pipeline_helpers.api_client_as_internal_service(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as api:
        result = await pipeline_helpers.wait_settings_seed_followup(
            api,
            ctx,
            pipeline_helpers.DeployRunResult(**initial["result"]),
            # Zero budget: both halves of the follow-up deploy wait are already
            # past their deadline and must still read before reporting one.
            repair_budget=0,
            retry_budget=60,
            overall_budget=60,
            poll_interval=0,
        )

    expected = (
        "settings-seed follow-up deploy run deploy-poll-skipped performed no "
        "deployment: already_deployed_same_sha"
    )
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair["id"], "status": "completed", "error": expected}
    ]


@pytest.mark.asyncio
async def test_a_story_parked_as_the_deadline_expires_names_the_refusal_not_a_timeout(monkeypatch):
    """The clock may answer only after every fact source was read and said nothing.

    The repair Run stays `running`, the story durably reaches
    `waiting_human_review`, and the repair budget is already spent — all in one
    interval. The wait used to test the deadline before it consulted the story,
    so it recorded "manifest repair attempt 1 timed out" beside a refusal that
    was sitting there to be read.
    """
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
        "status": "running",
        "run_metadata": {"deploy_fix_attempt": 1},
    }
    story_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal story_reads
        if request.url.path == "/api/system-configs/deploy.max_deploy_fix_attempts":
            return httpx.Response(200, json={"value": 2})
        if request.url.path == f"/api/runs/{repair['id']}":
            return httpx.Response(200, json=repair)
        if request.url.path == "/api/stories/story-1":
            story_reads += 1
            return httpx.Response(200, json={"status": "waiting_human_review"})
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
            api,
            ctx,
            failed,
            # Already spent: the terminal wait is past its deadline on its first
            # pass and must still read the Run and then the story.
            repair_budget=0,
            retry_budget=60,
            overall_budget=60,
            poll_interval=0,
        )

    expected = "story story-1 reached waiting_human_review before manifest repair attempt 1"
    assert result is None
    assert ctx["settings_seed_repair_error"] == expected
    assert ctx["settings_seed_repair_story_status"] == "waiting_human_review"
    assert ctx["settings_seed_repair_attempts"] == [
        {"attempt": 1, "run_id": repair["id"], "status": "running", "error": expected}
    ]
    assert story_reads == 1
