"""Offline contracts for mega-noop's completed-story and undeploy lifecycle."""

import httpx
import pipeline_helpers
import pytest

from shared.contracts.dto.application import ApplicationStatus

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
                            "task_id": "",
                        },
                    ]
                ],
            ),
        )

    assert result is not None
    assert ctx["owner_notification_po_event"]["text"] == notification["text"]


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
