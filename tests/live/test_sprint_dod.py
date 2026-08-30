"""Live Definition-of-Done proof for the ownership and recovery sprint.

This is deliberately its own stand target.  The normal and operator routes share
one deterministic deployment; the restart route creates a separate LLM project
and refuses a noop or host-session substitute in its assertions.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from typing import Any

from live_harness import cleanup_guard
from pipeline_helpers import (
    ENGINEERING_TIMEOUT,
    LLM_ENGINEERING_TIMEOUT,
    ORCHESTRATOR_ROOT,
    SCAFFOLD_TIMEOUT,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    cleanup_all,
    create_llm_backend_project,
    create_noop_project,
    create_story_and_task,
    ensure_test_user,
    live_worker_agent_type,
    trigger_scaffold,
    wait_engineering,
    wait_scaffold,
)
import pytest
import pytest_asyncio
from test_full_pipeline import _pipeline_run

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.po import POSystemEvent
from shared.contracts.worker_turn import AttemptTurnMetadata, WorkerActiveTurn
from shared.live_contour import require_live_contour
from shared.queues import PO_INPUT_QUEUE, STORY_WORKERS_KEY

pytestmark = pytest.mark.asyncio(loop_scope="module")

PO_INPUT = PO_INPUT_QUEUE
OPERATOR = "live-dod-operator"
OWNER_NOTIFICATION_TIMEOUT = 180
RESTART_TASK_TITLE = "Add backend ping endpoint"
RESTART_TASK_DESCRIPTION = (
    "Add a GET /ping endpoint to the scaffolded backend service that returns HTTP 200 with the "
    'JSON body {"pong": true}, plus a unit test for it. Keep GET /health unchanged, keep the '
    "project backend-only and require no user-provided secrets. Commit and push the change."
)
# A cold stand may need the full noop engineering budget before the first
# worker records its turn: dispatcher tick, image pull, container start, lease.
TURN_OBSERVATION_TIMEOUT = ENGINEERING_TIMEOUT


def _redis_json(*args: str) -> Any:
    """Read one Redis command through the stand's own Redis container."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", "--json", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "null")


def _redis_text(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", "--raw", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _stream_cursor(stream: str) -> str:
    entries = _redis_json("XREVRANGE", stream, "+", "-", "COUNT", "1")
    return entries[0][0] if entries else "0-0"


def _po_events_after(cursor: str) -> list[POSystemEvent]:
    entries = _redis_json("XRANGE", PO_INPUT, f"({cursor}", "+")
    events = []
    for _entry_id, fields in entries:
        flat_fields = _flat_fields(fields)
        if flat_fields.get("type") != "system_event":
            continue
        events.append(POSystemEvent.model_validate(flat_fields))
    return events


async def _wait_for_story_status(api, story_id: str, status: StoryStatus) -> dict:
    deadline = time.monotonic() + OWNER_NOTIFICATION_TIMEOUT
    while time.monotonic() < deadline:
        response = await api.get(f"/api/stories/{story_id}")
        response.raise_for_status()
        story = response.json()
        if story["status"] == status.value:
            return story
        await asyncio.sleep(3)
    raise AssertionError(f"story {story_id} did not reach {status.value}")


async def _wait_for_owner_instruction(
    api_internal, story_id: str, cursor: str
) -> tuple[dict, POSystemEvent]:
    deadline = time.monotonic() + OWNER_NOTIFICATION_TIMEOUT
    while time.monotonic() < deadline:
        notification_response = await api_internal.get(
            f"/api/stories/{story_id}/owner-notification"
        )
        notification_response.raise_for_status()
        notification = notification_response.json()
        matching = [
            event
            for event in _po_events_after(cursor)
            if event.story_id == story_id and event.event == "story_completed"
        ]
        if notification["state"] == OwnerNotificationState.DELIVERED.value and matching:
            return notification, matching[-1]
        await asyncio.sleep(3)
    raise AssertionError(f"story {story_id} never delivered its owner instruction to {PO_INPUT}")


def _worker_inventory() -> list[dict[str, Any]]:
    script = (
        "import json, urllib.request; "
        "print(urllib.request.urlopen("
        "'http://localhost:8000/api/introspect/workers/', timeout=10).read().decode())"
    )
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "worker-manager", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=ORCHESTRATOR_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


async def _wait_for_active_turn(project_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TURN_OBSERVATION_TIMEOUT
    while time.monotonic() < deadline:
        matches = [
            worker for worker in _worker_inventory() if worker.get("project_id") == project_id
        ]
        for worker in matches:
            if worker.get("active_turn_lease") is not None:
                return worker
        await asyncio.sleep(2)
    raise AssertionError(f"no active engineering turn was visible for project {project_id}")


async def _start_then_park_for_human_review(api, story_id: str) -> None:
    """Reach the review queue through the story transitions the product exposes."""
    started = await api.post(f"/api/stories/{story_id}/start")
    started.raise_for_status()
    parked = await api.post(f"/api/stories/{story_id}/human-review")
    parked.raise_for_status()


def _assert_no_orphan_workers(
    inventory: list[dict[str, Any]], project_id: str, story_id: str
) -> None:
    """Only live turn holders need a story binding; terminal history may outlive one."""
    for entry in inventory:
        if entry.get("project_id") != project_id:
            continue
        holds_live_turn = (
            entry.get("active_turn_lease") is not None or entry.get("waiting_attempt") is not None
        )
        if holds_live_turn:
            assert story_id in entry.get("story_bindings", []), (
                f"orphan worker inventory entry: {entry}"
            )


def _hgetall(key: str) -> dict[str, str]:
    return _flat_fields(_redis_json("HGETALL", key))


def _flat_fields(fields: dict[str, str] | list[str]) -> dict[str, str]:
    """Normalize redis-cli's RESP3 object and RESP2 flat-array JSON forms."""
    if isinstance(fields, dict):
        return {str(key): str(value) for key, value in fields.items()}
    return dict(zip(fields[::2], fields[1::2], strict=True))


def _fenced_terminal_attempt(
    attempts: list[dict[str, Any]], active_turn_request_id: str
) -> dict[str, Any]:
    """Find the one settled run that adopted the turn fenced before restart."""
    terminal_statuses = {"completed", "failed", "cancelled"}
    terminal_attempts = [
        attempt for attempt in attempts if attempt.get("status") in terminal_statuses
    ]
    fenced_attempts = [
        attempt
        for attempt in terminal_attempts
        if attempt.get("run_metadata", {}).get("active_turn_request_id") == active_turn_request_id
    ]
    if len(fenced_attempts) == 1:
        return fenced_attempts[0]

    observed_request_ids = [
        attempt.get("run_metadata", {}).get("active_turn_request_id")
        for attempt in terminal_attempts
    ]
    if not fenced_attempts and any(
        request_id and request_id != active_turn_request_id for request_id in observed_request_ids
    ):
        raise AssertionError(
            "fenced engineering turn was re-dispatched instead of adopted: "
            f"expected request_id={active_turn_request_id}, "
            f"terminal request_ids={observed_request_ids}"
        )
    if not fenced_attempts:
        raise AssertionError(
            "fenced engineering turn did not settle: "
            f"expected request_id={active_turn_request_id}, "
            f"terminal request_ids={observed_request_ids}. A retry after an ordinary "
            "failure remains valid only when the fenced request is also retained."
        )
    raise AssertionError(
        "fenced engineering turn settled more than once: "
        f"request_id={active_turn_request_id}, attempts={fenced_attempts}"
    )


def _restart_engineering_consumer() -> None:
    result = subprocess.run(
        ["docker", "compose", "restart", "engineering-worker"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ORCHESTRATOR_ROOT,
    )
    assert result.returncode == 0, result.stderr


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def normal_route():
    """One ordinary supervisor completion, retained for the operator route too."""
    cursor = _stream_cursor(PO_INPUT)
    async for ctx in _pipeline_run(
        # The ordinary route is intentionally mechanical.  The separate restart
        # scenario below is the one that must exercise an LLM coding turn.
        create_noop_project,
        engineering_timeout=ENGINEERING_TIMEOUT,
        debug_prefix="sprint-dod-normal",
    ):
        ctx["po_input_cursor"] = cursor
        yield ctx


async def test_normal_route_completes_and_tells_po_the_qa_verified_address(normal_route):
    """(a) Ordinary QA completion reaches PO as a stored system instruction."""
    ctx = normal_route
    assert ctx.get("scaffold_status") == ProjectStatus.ACTIVE
    assert ctx.get("task_status") == TaskStatus.DONE
    assert ctx.get("final_app_status") == ApplicationStatus.RUNNING.value
    assert ctx.get("qa_result", {}).get("qa_outcome") == "passed"
    assert ctx.get("deployed_url"), "a passed QA route must expose its verified address"

    async with api_client_as_internal_service() as api_internal:
        await _wait_for_story_status(api_internal, ctx["story_id"], StoryStatus.COMPLETED)
        notification, event = await _wait_for_owner_instruction(
            api_internal, ctx["story_id"], ctx["po_input_cursor"]
        )

    # The test never constructs user-facing wording.  It compares PO's input to
    # the completion transaction's durable instruction instead.
    assert event.text == notification["text"]
    assert event.event == notification["event"] == "story_completed"
    assert event.project_id == ctx["project_id"]
    assert ctx["deployed_url"] in event.text


async def test_operator_acceptance_is_audited_and_notified(normal_route):
    """(b) accept-result is audited and shares the completion delivery with the normal route.

    The stopping-target refusal is not proved here: it fires only for a story whose
    current cycle recorded a QA hand-off, and no product transition returns a completed
    story to review with that hand-off intact (reopen filters earlier QA runs by
    ``reopened_at``). Its coverage is the offline service test in
    ``services/api/tests/service/test_story_recheck.py``.
    """
    ctx = normal_route
    async with (
        api_client_as_test_user() as api,
        api_client_as_internal_service() as api_internal,
    ):
        # A fresh reviewed story isolates the audit assertion from the normal
        # completion above while retaining the run-owned project's cleanup.
        created = await api.post(
            "/api/stories/",
            json={
                "project_id": ctx["project_id"],
                "title": "Operator-reviewed result",
                "description": "A result awaiting operator acceptance.",
                "type": "technical",
            },
        )
        created.raise_for_status()
        reviewed_story_id = created.json()["id"]
        await _start_then_park_for_human_review(api, reviewed_story_id)

        cursor = _stream_cursor(PO_INPUT)
        accepted = await api_internal.post(
            f"/api/stories/{reviewed_story_id}/accept-result",
            headers={"X-Admin-Console-Operator": OPERATOR},
            json={"basis": "release evidence reviewed"},
        )
        accepted.raise_for_status()
        accepted_story = accepted.json()
        acceptance = accepted_story["operator_acceptance"]
        assert acceptance["actor"] == f"admin_console:{OPERATOR}"
        assert acceptance["basis"] == "release evidence reviewed"
        assert acceptance["accepted_at"]

        notification, event = await _wait_for_owner_instruction(
            api_internal, reviewed_story_id, cursor
        )
        assert event.text == notification["text"]
        assert event.event == notification["event"] == "story_completed"


@pytest.mark.live_llm_stand_token
async def test_restart_mid_llm_turn_preserves_one_attempt_and_leaves_no_orphans():
    """(c) A real stand-token coding turn survives an engineering consumer restart."""
    contour = require_live_contour()
    assert contour.name == "stand", "this test is a stand-only LLM contour"
    requested_agent = live_worker_agent_type()
    assert requested_agent in {"claude", "codex"}, "the restart proof must not use a noop worker"

    async with (
        api_client_as_test_user() as api,
        api_client_as_internal_service() as api_internal,
        api_client_as_unscoped_observer() as api_observer,
    ):
        await ensure_test_user(api, api_internal)
        ctx = await create_llm_backend_project(api, api_internal)
        async with cleanup_guard(
            lambda: cleanup_all(api_internal, api_observer, ctx), manifest=ctx["manifest"]
        ):
            trigger_scaffold(ctx)
            await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)
            assert ctx.get("scaffold_status") == ProjectStatus.ACTIVE
            # The shared health task can be honestly closed with no commit once the
            # scaffold already ships GET /health, and a turn that lands no commit is
            # judged failed regardless of adoption. This proof needs a turn whose
            # result is a real push, so it asks for an endpoint the scaffold lacks.
            ctx["task_title"] = RESTART_TASK_TITLE
            ctx["task_description"] = RESTART_TASK_DESCRIPTION
            await create_story_and_task(api, ctx)

            worker = await _wait_for_active_turn(ctx["project_id"])
            worker_id = worker["id"]
            active_turn = WorkerActiveTurn.from_redis_fields(
                _hgetall(f"worker:active-turn:{worker_id}")
            )
            assert active_turn is not None
            attempt_response = await api_internal.get(f"/api/runs/{active_turn.attempt_id}")
            attempt_response.raise_for_status()
            attempt_turn = AttemptTurnMetadata.from_run_metadata(
                attempt_response.json()["run_metadata"]
            )
            assert attempt_turn.worker_id == worker_id
            assert attempt_turn.active_turn_request_id == active_turn.request_id
            assert worker["active_turn_lease"]["request_id"] == active_turn.request_id
            # The worker-manager must derive this waiter from the durable
            # AttemptTurnMetadata, not merely from Docker or a worker status.
            assert worker["waiting_attempt"]["request_id"] == active_turn.request_id
            assert worker["waiting_attempt"]["run_id"] == active_turn.attempt_id
            assert worker["story_bindings"] == [ctx["story_id"]]
            assert _redis_text("HGET", STORY_WORKERS_KEY, ctx["story_id"]) == worker_id
            assert _hgetall(f"worker:meta:{worker_id}")["auth_mode"] == "stand_token"

            # The manager, rather than this test or its queue message, owns the
            # secret.  Check presence only, so no credential reaches test logs.
            internal_key = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "worker-manager",
                    "python",
                    "-c",
                    "import os; raise SystemExit(not bool(os.environ.get('INTERNAL_API_KEY')))",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=ORCHESTRATOR_ROOT,
            )
            assert internal_key.returncode == 0, "worker-manager has no INTERNAL_API_KEY"

            _restart_engineering_consumer()

            # The broker lease remains after the old consumer is cancelled.  It
            # fences the replacement to this exact prompt rather than a health
            # heuristic or a second publication.
            surviving = WorkerActiveTurn.from_redis_fields(
                _hgetall(f"worker:active-turn:{worker_id}")
            )
            assert surviving == active_turn

            await wait_engineering(api, ctx, timeout=LLM_ENGINEERING_TIMEOUT)
            assert ctx.get("task_status") == TaskStatus.DONE, ctx

            runs_response = await api_internal.get(
                "/api/runs/", params={"project_id": ctx["project_id"], "run_type": "engineering"}
            )
            runs_response.raise_for_status()
            attempts = [
                run
                for run in runs_response.json()
                if run.get("task_id") == ctx["task_id"] and run.get("type") == "engineering"
            ]
            attempt = _fenced_terminal_attempt(attempts, active_turn.request_id)
            assert attempt["run_metadata"]["active_turn_request_id"] == active_turn.request_id
            output = _redis_text("XRANGE", f"worker:{worker_id}:output", "-", "+")
            assert active_turn.request_id in output, (
                "replacement did not reclaim retained broker output"
            )

            inventory = {entry["id"]: entry for entry in _worker_inventory()}
            holder = _redis_text("GET", f"workspace:lock:{ctx['project_id']}")
            if holder:
                assert holder in inventory, f"orphan workspace lock held by {holder}"
                assert ctx["story_id"] in inventory[holder]["story_bindings"], (
                    f"workspace lock holder {holder} is not bound to the story"
                )
            _assert_no_orphan_workers(list(inventory.values()), ctx["project_id"], ctx["story_id"])
