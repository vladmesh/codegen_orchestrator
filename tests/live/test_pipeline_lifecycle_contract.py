"""Offline contracts for mega-noop's completed-story and undeploy lifecycle."""

import httpx
import pipeline_helpers
import pytest

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.task import TaskStatus

pytestmark = pytest.mark.needs_no_api_credential


def _client(handler):
    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_story_completion_records_terminal_fact_and_timeout_diagnostic():
    ctx = {"story_id": "story-1"}

    async with _client(lambda _request: httpx.Response(200, json={"status": "completed"})) as api:
        story = await pipeline_helpers.wait_story_completed(api, ctx, timeout=1)

    assert story == {"status": "completed"}
    assert ctx["story_terminal"] == story

    async with _client(lambda _request: httpx.Response(200, json={"status": "testing"})) as api:
        assert (
            await pipeline_helpers.wait_story_completed(api, ctx, timeout=0.001, poll_interval=0)
        ) is None

    assert "last_status=testing" in ctx["story_terminal_error"]


def test_po_cursor_and_events_exclude_history_and_type_the_new_system_event():
    def command(*args):
        if args[0] == "XREVRANGE":
            return [["10-0", {"type": "system_event"}]]
        assert args == ("XRANGE", "po:input", "(10-0", "+")
        return [
            [
                "11-0",
                {
                    "type": "user_message",
                    "text": "foreign",
                    "telegram_chat_id": "1",
                    "request_id": "x",
                },
            ],
            [
                "12-0",
                {
                    "type": "system_event",
                    "event": "story_completed",
                    "text": "done http://198.51.100.2:8010",
                    "story_id": "story-1",
                    "project_id": "project-1",
                    "task_id": "",
                },
            ],
        ]

    assert pipeline_helpers.po_input_cursor(command=command) == "10-0"
    events = pipeline_helpers.po_events_after("10-0", command=command)
    assert len(events) == 1
    assert events[0].story_id == "story-1"


_RUN_OWNER = pipeline_helpers.RunOwner(
    telegram_id=990_000_123,
    user_id=7,
    promo_code="spent",
    credits_microusd=pipeline_helpers.LEVEL1_PROMO_CREDITS_MICROUSD,
    attempt_reservation_microusd=pipeline_helpers.LEVEL1_PROMO_ATTEMPT_RESERVATION_MICROUSD,
)


def _settlement_ctx(agent_type: str) -> dict:
    return {
        "project_id": "project-1",
        "story_id": "story-1",
        "task_ids": ["task-1", "task-2"],
        "agent_type": agent_type,
        "run_owner": _RUN_OWNER,
    }


def _settlement_api(agent_type: str, *, cost_microusd: int | None = None, null_cost_task=None):
    """A stand's durable paid-work facts for two engineering Runs, one per task.

    A noop attempt is unenforced and carries an unknown-cost ledger row. A model
    attempt is admitted under the run owner's promo policy, settled, and carries
    the cost its provider reported — except on `null_cost_task`, whose provider
    reported none, so its reservation stayed `unknown_final`.
    """

    def run(task_id):
        return {
            "id": f"eng-{task_id}",
            "type": "engineering",
            "status": "completed",
            "project_id": "project-1",
            "story_id": "story-1",
            "task_id": task_id,
            "run_metadata": {
                "triggered_by": "dispatcher",
                "executor_decision": {
                    "attempt_kind": "engineering",
                    "agent_type": agent_type,
                    "source": "project_pin",
                    "policy_version": "v2",
                    "reason": f"project config pins engineering executor to {agent_type}",
                },
            },
            "result": {"engineering_status": "done", "commit_sha": "abc123"},
        }

    def ledger(run_id, task_id):
        row = {
            "id": "00000000-0000-0000-0000-000000000001",
            "idempotency_key": f"engineering-run:{run_id}",
            "run_id": run_id,
            "project_id": "project-1",
            "story_id": "story-1",
            "task_id": task_id,
            "user_id": 7,
            "owner_attribution": "resolved",
            "role": "engineering",
            "occurred_at": "2026-08-31T00:00:00Z",
            "provider": None,
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_microusd": None,
            "cost_source": "unknown",
        }
        if agent_type != "noop" and task_id != null_cost_task:
            row.update(
                provider="anthropic",
                model="claude-sonnet-5",
                input_tokens=1000,
                output_tokens=200,
                total_tokens=1200,
                cost_microusd=cost_microusd,
                cost_source="provider_reported",
            )
        return row

    def reservation(run_id, task_id):
        if agent_type == "noop":
            return {
                "attempt_id": run_id,
                "user_id": 7,
                "outcome": "unlimited",
                "reservation_microusd": 0,
                "known_spend_microusd": 0,
                "active_held_microusd": 0,
                "available_microusd": None,
                "reservation_state": None,
            }
        settled = task_id != null_cost_task
        reserve = _RUN_OWNER.attempt_reservation_microusd
        return {
            "attempt_id": run_id,
            "user_id": 7,
            "outcome": "admitted",
            "reservation_microusd": reserve,
            "known_spend_microusd": 0,
            "active_held_microusd": 0 if settled else reserve,
            "available_microusd": _RUN_OWNER.credits_microusd,
            "reservation_state": "settled" if settled else "unknown_final",
        }

    def handler(request):
        path = request.url.path
        if path == "/api/runs/":
            return httpx.Response(200, json=[run(request.url.params["task_id"])])
        if path.startswith("/api/runs/engineering-attempts"):
            run_id = request.url.params["run_id"]
            return httpx.Response(200, json=[ledger(run_id, run_id.removeprefix("eng-"))])
        if path.startswith("/api/runs/eng-"):
            return httpx.Response(200, json=run(path.removeprefix("/api/runs/eng-")))
        if path.startswith("/api/work-admission/paid-runs/eng-"):
            return httpx.Response(
                200,
                json={"outcome": "admitted", "reason": None, "retryable": False, "message": None},
            )
        if path.startswith("/api/engineering-budget-policies/admissions/eng-"):
            run_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=reservation(run_id, run_id.removeprefix("eng-")))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return _client(handler)


@pytest.mark.asyncio
async def test_noop_settlement_evidence_is_typed_per_admitted_engineering_run():
    """Noop acceptance reads durable paid-admission, result, ledger and reservation facts."""
    ctx = _settlement_ctx("noop")

    async with _settlement_api("noop") as api:
        evidence = await pipeline_helpers.record_engineering_settlement_evidence(api, ctx)

    assert set(evidence) == {"eng-task-1", "eng-task-2"}
    assert ctx["engineering_settlement_error"] is None
    assert all(item["admission"]["outcome"] == "admitted" for item in evidence.values())
    assert all(item["ledger"]["cost_source"] == "unknown" for item in evidence.values())
    assert all(item["ledger"]["cost_microusd"] is None for item in evidence.values())


@pytest.mark.asyncio
async def test_a_claude_developer_s_settlement_carries_its_provider_cost():
    """Level 2: the requested developer, a reported cost and a settled promo reservation."""
    ctx = _settlement_ctx("claude")

    async with _settlement_api("claude", cost_microusd=1_234_567) as api:
        evidence = await pipeline_helpers.record_engineering_settlement_evidence(api, ctx)

    assert ctx["engineering_settlement_error"] is None
    assert set(evidence) == {"eng-task-1", "eng-task-2"}
    for item in evidence.values():
        assert item["decision"]["agent_type"] == "claude"
        assert item["ledger"]["cost_source"] == "provider_reported"
        assert item["ledger"]["cost_microusd"] == 1_234_567
        assert item["reservation"]["reservation_state"] == "settled"
        assert item["reservation"]["user_id"] == _RUN_OWNER.user_id


@pytest.mark.asyncio
async def test_a_model_run_without_a_provider_cost_is_refused_by_name():
    """A null cost is not a cheap attempt, it is an unknown one: the run is named.

    This is what a Codex developer looks like today — it reports no cost — and
    level 2 refuses it on purpose rather than settling for `unknown`.
    """
    ctx = _settlement_ctx("codex")

    async with _settlement_api("codex", cost_microusd=900_000, null_cost_task="task-2") as api:
        evidence = await pipeline_helpers.record_engineering_settlement_evidence(api, ctx)

    assert evidence == {}
    assert ctx["engineering_settlement"] == {}
    assert "eng-task-2" in ctx["engineering_settlement_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_type", "cost", "passes"),
    [("claude", 125, True), ("claude", None, False), ("noop", None, True)],
)
async def test_qa_settlement_requires_live_cost_and_noop_release(agent_type, cost, passes):
    ctx = _settlement_ctx(agent_type)
    ctx["qa_run"] = {"id": "qa-story-1", "status": "completed"}

    def handler(request):
        if request.url.path == "/api/runs/engineering-attempts":
            rows = (
                []
                if agent_type == "noop"
                else [
                    {
                        "run_id": "qa-story-1",
                        "role": "qa",
                        "user_id": 7,
                        "project_id": "project-1",
                        "story_id": "story-1",
                        "task_id": None,
                        "cost_source": "provider_reported" if cost is not None else "unknown",
                        "cost_microusd": cost,
                    }
                ]
            )
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("/admissions/qa-story-1"):
            return httpx.Response(
                200,
                json={
                    "attempt_id": "qa-story-1",
                    "user_id": 7,
                    "outcome": "admitted",
                    "reservation_microusd": 5_000_000,
                    "known_spend_microusd": 0,
                    "active_held_microusd": 0,
                    "available_microusd": 90_000_000,
                    "reservation_state": "released" if agent_type == "noop" else "settled",
                },
            )
        raise AssertionError(request.url)

    async with _client(handler) as api:
        evidence = await pipeline_helpers.record_qa_settlement_evidence(api, ctx)
    assert bool(evidence) is passes
    if not passes:
        assert "qa-story-1" in ctx["qa_settlement_error"]


@pytest.mark.asyncio
async def test_a_model_run_decided_for_another_developer_is_refused():
    """The persisted decision must be the developer this run asked for."""
    ctx = _settlement_ctx("codex")

    async with _settlement_api("claude", cost_microusd=1) as api:
        await pipeline_helpers.record_engineering_settlement_evidence(api, ctx)

    assert "eng-task-1" in ctx["engineering_settlement_error"]
    assert "codex" in ctx["engineering_settlement_error"]


@pytest.mark.asyncio
async def test_linear_noop_wait_keeps_the_dependent_task_and_pr_fenced(monkeypatch):
    """The live poll observes the dependency until task one is terminal."""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(pipeline_helpers.asyncio, "sleep", no_sleep)
    ctx = {"story_id": "story-1", "first_task_id": "task-1", "second_task_id": "task-2"}
    reads = {"first": 0, "second": 0}

    def engineering_run(task_id):
        return {
            "id": f"eng-{task_id}",
            "type": "engineering",
            "task_id": task_id,
            "run_metadata": {
                "executor_decision": {
                    "attempt_kind": "engineering",
                    "agent_type": "noop",
                    "source": "project_pin",
                    "policy_version": "v2",
                    "reason": "test noop pin",
                }
            },
        }

    def handler(request):
        if request.url.path == "/api/tasks/task-1":
            reads["first"] += 1
            return httpx.Response(200, json={"status": "in_dev" if reads["first"] == 1 else "done"})
        if request.url.path == "/api/tasks/task-2":
            reads["second"] += 1
            return httpx.Response(200, json={"status": "done" if reads["second"] >= 3 else "todo"})
        if request.url.path == "/api/stories/story-1":
            return httpx.Response(200, json={"status": "pr_review", "pr_number": None})
        if request.url.path == "/api/runs/":
            task_id = request.url.params["task_id"]
            if task_id == "task-2" and reads["second"] < 3:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[engineering_run(task_id)])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _client(handler) as api:
        await pipeline_helpers.wait_linear_noop_engineering(api, api, ctx, timeout=10)

    assert ctx["first_task_status"] == TaskStatus.DONE
    assert ctx["second_task_status"] == TaskStatus.DONE
    assert ctx.get("noop_task_sequence_error") is None
    assert set(ctx["engineering_dispatch_decisions"]) == {"eng-task-1", "eng-task-2"}


def _bot_notification(text: str) -> dict:
    return {
        "event": "story_completed",
        "text": text,
        "story_id": "story-1",
        "project_id": "project-1",
        "terminal_status": "completed",
        "task_id": None,
        "state": "delivered",
    }


def _po_event(text: str):
    return lambda _cursor: pipeline_helpers.po_events_after(
        "10-0",
        command=lambda *_args: [
            [
                "12-0",
                {
                    "type": "system_event",
                    "event": "story_completed",
                    "text": text,
                    "story_id": "story-1",
                    "project_id": "project-1",
                    # Story-level notifications use the story as the PO subject
                    # even though the durable record has no task.
                    "task_id": "story-1",
                },
            ]
        ],
    )


#: What the level-1 product's owner is told, as the API composes it. The path no
#: longer demands the backend address: this is a bot product, and its owner is
#: given the bot instead (`_telegram_bot_usage_instructions`).
BOT_COMPLETION_TEXT = (
    "The story is finished: it is deployed and QA passed. They reach the Telegram bot "
    "@mega_e2e_codegen_bot (https://t.me/mega_e2e_codegen_bot)."
)


def _names_the_bot(text: str) -> list[str]:
    return [] if "@mega_e2e_codegen_bot" in text else ["it does not name the bot"]


@pytest.mark.asyncio
async def test_owner_notification_requires_durable_identity_message_and_new_po_event():
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    notification = _bot_notification(BOT_COMPLETION_TEXT)

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api,
            ctx,
            timeout=1,
            text_requirement=_names_the_bot,
            events_after=_po_event(BOT_COMPLETION_TEXT),
        )

    assert result is not None
    assert ctx["owner_notification_po_event"]["text"] == notification["text"]
    assert ctx["owner_notification_po_event"]["task_id"] == "story-1"


@pytest.mark.asyncio
async def test_a_message_the_caller_refuses_is_named_as_the_message():
    """The text requirement is the level-1 path's own, and a timeout says so.

    Run 35441716423 delivered its notification and was rejected; its artifact
    could not say whether the message or the durable PO record was what did not
    match. Both are reported now, separately.
    """
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    backend_text = "Done: http://198.51.100.2:8010"
    notification = _bot_notification(backend_text)

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api,
            ctx,
            timeout=0.001,
            poll_interval=0,
            text_requirement=_names_the_bot,
            events_after=_po_event(backend_text),
        )

    assert result is None
    error = ctx["owner_notification_error"]
    assert "The notification itself: text: it does not name the bot" in error
    assert "The durable PO record: matched" in error


@pytest.mark.asyncio
async def test_a_missing_po_record_is_named_as_the_po_record():
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    notification = _bot_notification(BOT_COMPLETION_TEXT)

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api,
            ctx,
            timeout=0.001,
            poll_interval=0,
            text_requirement=_names_the_bot,
            events_after=lambda _cursor: [],
        )

    assert result is None
    error = ctx["owner_notification_error"]
    assert "events_after_cursor=0" in error
    assert "The notification itself: every field matched" in error
    assert "none of the 0 new PO events is a story_completed event for story story-1" in error


@pytest.mark.asyncio
async def test_a_po_record_whose_text_differs_says_which_field_differs():
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    notification = _bot_notification(BOT_COMPLETION_TEXT)

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api,
            ctx,
            timeout=0.001,
            poll_interval=0,
            text_requirement=_names_the_bot,
            events_after=_po_event("some other completion message"),
        )

    assert result is None
    error = ctx["owner_notification_error"]
    assert "The notification itself: every field matched" in error
    assert "none matches the notification on: text" in error


def _deployment(identifier, *, sha="abc123", result="success", deployed_at="2026-09-19T15:48:00"):
    return {
        "id": identifier,
        "application_id": 7,
        "project_id": "project-1",
        "result": result,
        "deployed_sha": sha,
        "deployed_at": deployed_at,
    }


def _deployment_and_runs(deployments, runs):
    """Answer the two reads `wait_service_deployment` takes on every pass."""
    seen = {}

    def handler(request):
        seen.setdefault("params", {})[request.url.path] = dict(request.url.params)
        body = runs if request.url.path == "/api/runs/" else deployments
        return httpx.Response(200, json=body)

    return handler, seen


@pytest.mark.asyncio
async def test_service_deployment_selects_exact_application_project_and_sha():
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    handler, seen = _deployment_and_runs([_deployment(4)], [{"id": "deploy-1", "type": "deploy"}])

    async with _client(handler) as api:
        deployment = await pipeline_helpers.wait_service_deployment(api, ctx, timeout=1)

    assert deployment["id"] == 4
    assert seen["params"]["/api/service-deployments/"] == {
        "application_id": "7",
        "project_id": "project-1",
    }
    assert seen["params"]["/api/runs/"] == {"project_id": "project-1", "run_type": "deploy"}


@pytest.mark.asyncio
async def test_two_deploys_of_one_application_are_a_log_not_an_ambiguity():
    """What run 35451082771 actually had, and what the old assertion called ambiguous.

    The story's own grant deploy wrote one record; the redeploy that hands QA
    its temporary capability wrote a second one for the same application, the
    same project and the same merged commit — `service_name` is the project's
    runtime slug in both, whatever the product's modules are. Two deploys, two
    records, and the one the application is serving is the later of them.
    """
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    handler, _ = _deployment_and_runs(
        [
            _deployment(1, deployed_at="2026-09-19T15:48:00"),
            _deployment(2, deployed_at="2026-09-19T15:49:14"),
        ],
        [
            {"id": "deploy-grant-7167257310", "type": "deploy"},
            {"id": "temporary-access-grant-a6a508be", "type": "deploy"},
        ],
    )

    async with _client(handler) as api:
        deployment = await pipeline_helpers.wait_service_deployment(api, ctx, timeout=1)

    assert deployment["id"] == 2


@pytest.mark.asyncio
async def test_a_deploy_recorded_twice_is_still_refused():
    """The guard the one-row assertion existed for, kept.

    The records carry no run id, so the duplicate is caught by counting: two
    records for one deploy is one deploy recorded twice, and the selection says
    which records and which deploys it counted.
    """
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    handler, _ = _deployment_and_runs(
        [_deployment(4), _deployment(5)], [{"id": "deploy-1", "type": "deploy"}]
    )

    async with _client(handler) as api:
        assert await pipeline_helpers.wait_service_deployment(api, ctx, timeout=1) is None

    error = ctx["service_deployment_error"]
    assert "more deployment records than deploys" in error
    assert "[4, 5]" in error
    assert "deploy-1" in error


@pytest.mark.asyncio
async def test_an_undeploy_run_is_not_counted_as_a_deploy_that_records():
    """A stop or undeploy never reaches the deployer, so it writes no record."""
    runs = [
        {"id": "deploy-1", "type": "deploy", "result": {"action": "feature"}},
        {"id": "undeploy-1", "type": "deploy", "result": {"action": "undeploy"}},
    ]

    assert [run["id"] for run in pipeline_helpers.deploys_that_record(runs)] == ["deploy-1"]


@pytest.mark.asyncio
async def test_service_deployment_rejects_a_record_of_another_sha():
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    handler, _ = _deployment_and_runs(
        [_deployment(4, sha="def456")], [{"id": "deploy-1", "type": "deploy"}]
    )

    async with _client(handler) as api:
        assert (
            await pipeline_helpers.wait_service_deployment(api, ctx, timeout=0.001, poll_interval=0)
            is None
        )

    assert "deployed_sha=def456" in ctx["service_deployment_error"]


@pytest.mark.asyncio
async def test_undeploy_request_associates_only_a_new_application_run(monkeypatch):
    ctx = {"application_id": 7, "project_id": "project-1"}
    monkeypatch.setattr(pipeline_helpers, "require_unscoped_run_observer", lambda _api: None)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "deploy-old"}])
        return httpx.Response(200, json={"id": 7, "status": "undeploying"})

    async with _client(handler) as api:
        await pipeline_helpers.request_undeploy(api, api, ctx)

    assert ctx["deploy_run_ids_before_undeploy"] == {"deploy-old"}
    assert calls == [("GET", "/api/runs/"), ("POST", "/api/applications/7/undeploy")]

    runs = [
        {"id": "deploy-old", "status": "completed", "run_metadata": {"application_id": 7}},
        {"id": "deploy-new", "status": "completed", "run_metadata": {"application_id": 7}},
    ]
    async with _client(lambda _request: httpx.Response(200, json=runs)) as api:
        run = await pipeline_helpers.wait_undeploy_run(api, ctx, timeout=1)
    assert run["id"] == "deploy-new"


@pytest.mark.asyncio
async def test_not_deployed_and_port_residue_are_bounded_and_fail_closed():
    ctx = {"application_id": 7, "allocation_id": 31, "server_handle": "stand-1"}
    async with _client(
        lambda _request: httpx.Response(200, json={"id": 7, "status": "not_deployed"})
    ) as api:
        application = await pipeline_helpers.wait_application_not_deployed(api, ctx, timeout=1)
    assert application["status"] == ApplicationStatus.NOT_DEPLOYED.value

    async with _client(lambda _request: httpx.Response(200, json=[])) as api:
        residue = await pipeline_helpers.verify_undeploy_residue(api, ctx)
        assert residue["port_allocation_absent"] is True

    async with _client(
        lambda _request: httpx.Response(200, json=[{"id": 31, "application_id": 7}])
    ) as api:
        assert await pipeline_helpers.verify_undeploy_residue(api, ctx) is None
    assert "left owned port allocations" in ctx["undeploy_residue_error"]
