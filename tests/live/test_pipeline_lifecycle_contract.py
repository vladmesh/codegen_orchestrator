"""Offline contracts for mega-noop's completed-story and undeploy lifecycle."""

import json

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


@pytest.mark.asyncio
async def test_linear_noop_story_creates_a_todo_dependent_second_task(monkeypatch):
    """Both tasks are schedulable, but the second carries the first as its fence."""
    monkeypatch.setattr(pipeline_helpers, "own_deploy_ahead", lambda _ctx: None)
    ctx = {
        "project_id": "project-1",
        "task_title": "first noop",
        "task_description": "first deterministic noop",
    }
    task_payloads = []
    transitions = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/api/stories/":
            return httpx.Response(201, json={"id": "story-1"})
        if request.method == "POST" and request.url.path == "/api/stories/story-1/start":
            return httpx.Response(200, json={"id": "story-1"})
        if request.method == "POST" and request.url.path == "/api/tasks/":
            task_payloads.append(json.loads(request.content))
            return httpx.Response(201, json={"id": f"task-{len(task_payloads)}"})
        if request.method == "POST" and request.url.path.endswith("/transition"):
            transitions.append((request.url.path, request.url.params["to_status"]))
            return httpx.Response(200, json={"status": request.url.params["to_status"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _client(handler) as api:
        await pipeline_helpers.create_story_and_task(api, ctx, linear_noop_tasks=True)

    assert ctx["task_ids"] == ["task-1", "task-2"]
    assert ctx["task_id"] == ctx["first_task_id"] == "task-1"
    assert ctx["second_task_id"] == "task-2"
    assert task_payloads[0]["status"] == TaskStatus.BACKLOG
    assert task_payloads[1]["status"] == TaskStatus.BACKLOG
    assert task_payloads[1]["blocked_by_task_id"] == "task-1"
    assert transitions == [
        ("/api/tasks/task-1/transition", TaskStatus.TODO),
        ("/api/tasks/task-2/transition", TaskStatus.TODO),
    ]


@pytest.mark.asyncio
async def test_noop_settlement_evidence_is_typed_per_admitted_engineering_run():
    """Noop acceptance reads durable paid-admission, result, ledger and reservation facts."""
    ctx = {
        "project_id": "project-1",
        "story_id": "story-1",
        "task_ids": ["task-1", "task-2"],
        "agent_type": "noop",
    }

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
                    "agent_type": "noop",
                    "source": "project_pin",
                    "policy_version": "v2",
                    "reason": "project config pins engineering executor to noop",
                },
            },
            "result": {"engineering_status": "done", "commit_sha": "abc123"},
        }

    def handler(request):
        path = request.url.path
        if path == "/api/runs/":
            return httpx.Response(200, json=[run(request.url.params["task_id"])])
        if path.startswith("/api/runs/engineering-attempts"):
            run_id = request.url.params["run_id"]
            task_id = run_id.removeprefix("eng-")
            return httpx.Response(
                200,
                json=[
                    {
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
                ],
            )
        if path.startswith("/api/runs/eng-"):
            return httpx.Response(200, json=run(path.removeprefix("/api/runs/eng-")))
        if path.startswith("/api/work-admission/paid-runs/eng-"):
            return httpx.Response(
                200,
                json={"outcome": "admitted", "reason": None, "retryable": False, "message": None},
            )
        if path.startswith("/api/engineering-budget-policies/admissions/eng-"):
            run_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "attempt_id": run_id,
                    "user_id": 7,
                    "outcome": "unlimited",
                    "reservation_microusd": 0,
                    "known_spend_microusd": 0,
                    "active_held_microusd": 0,
                    "available_microusd": None,
                    "reservation_state": None,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _client(handler) as api:
        evidence = await pipeline_helpers.record_noop_settlement_evidence(api, ctx)

    assert set(evidence) == {"eng-task-1", "eng-task-2"}
    assert ctx["noop_settlement_error"] is None
    assert all(item["admission"]["outcome"] == "admitted" for item in evidence.values())
    assert all(item["ledger"]["cost_source"] == "unknown" for item in evidence.values())


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


@pytest.mark.asyncio
async def test_owner_notification_requires_durable_identity_url_and_new_po_event():
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    notification = {
        "event": "story_completed",
        "text": "Done: http://198.51.100.2:8010",
        "story_id": "story-1",
        "project_id": "project-1",
        "terminal_status": "completed",
        "task_id": None,
        "state": "delivered",
    }

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api,
            ctx,
            timeout=1,
            events_after=lambda _cursor: pipeline_helpers.po_events_after(
                "10-0",
                command=lambda *_args: [
                    [
                        "12-0",
                        {
                            "type": "system_event",
                            "event": "story_completed",
                            "text": "Done: http://198.51.100.2:8010",
                            "story_id": "story-1",
                            "project_id": "project-1",
                            # Story-level notifications use the story as the PO
                            # subject even though the durable record has no task.
                            "task_id": "story-1",
                        },
                    ]
                ],
            ),
        )

    assert result is not None
    assert ctx["owner_notification_po_event"]["text"] == notification["text"]
    assert ctx["owner_notification_po_event"]["task_id"] == "story-1"


@pytest.mark.asyncio
async def test_owner_notification_fails_closed_for_a_foreign_po_event():
    ctx = {
        "story_id": "story-1",
        "project_id": "project-1",
        "deployed_url": "http://198.51.100.2:8010",
        "po_input_cursor": "10-0",
    }
    notification = {
        "event": "story_completed",
        "text": "Done: http://198.51.100.2:8010",
        "story_id": "story-1",
        "project_id": "project-1",
        "terminal_status": "completed",
        "task_id": None,
        "state": "delivered",
    }

    async with _client(lambda _request: httpx.Response(200, json=notification)) as api:
        result = await pipeline_helpers.wait_owner_completion_notification(
            api, ctx, timeout=0.001, poll_interval=0, events_after=lambda _cursor: []
        )

    assert result is None
    assert "events_after_cursor=0" in ctx["owner_notification_error"]


@pytest.mark.asyncio
async def test_service_deployment_selects_exact_application_project_and_sha():
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=[
                {
                    "id": 4,
                    "application_id": 7,
                    "project_id": "project-1",
                    "result": "success",
                    "deployed_sha": "abc123",
                }
            ],
        )

    async with _client(handler) as api:
        deployment = await pipeline_helpers.wait_service_deployment(api, ctx, timeout=1)

    assert deployment["id"] == 4
    assert seen["params"] == {"application_id": "7", "project_id": "project-1"}


@pytest.mark.asyncio
async def test_service_deployment_rejects_ambiguous_or_wrong_sha_records():
    ctx = {"application_id": 7, "project_id": "project-1", "deploy_head_sha": "abc123"}
    ambiguous = [
        {
            "id": 4,
            "application_id": 7,
            "project_id": "project-1",
            "result": "success",
            "deployed_sha": "abc123",
        },
        {
            "id": 5,
            "application_id": 7,
            "project_id": "project-1",
            "result": "success",
            "deployed_sha": "abc123",
        },
    ]
    async with _client(lambda _request: httpx.Response(200, json=ambiguous)) as api:
        assert await pipeline_helpers.wait_service_deployment(api, ctx, timeout=1) is None
    assert "ambiguous" in ctx["service_deployment_error"]


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
