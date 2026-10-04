"""Health QA crosses real admission, Redis, consumer, terminal API and Story routing.

Only the deployed product's HTTP responses and the paid executor edge are
controlled. PostgreSQL, encrypted secrets, diagnostics and state transitions
use the existing control-api service fixture. No stand or model is launched.
"""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch
import uuid

import httpx
from psycopg import AsyncConnection
import pytest
import respx

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.work_admission import PaidRunStartCommand
from shared.contracts.queues.qa import QAMessage
from shared.contracts.vocab import AgentType
from shared.queues import QA_QUEUE, WORKER_COMMANDS
from shared.redis import RedisStreamClient
from shared.tests.executor_diagnostic_cases import host_profile_for_reason
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.clients.api import LanggraphAPIClient
from src.config.settings import get_settings
from src.consumers.qa import process_qa_job

PRODUCT = "https://health-product.test"
CAPABILITIES = {
    "USER_IDENTITY_CAPABILITY": "synthetic-health-identity-canary",
    "USERS_GRANT_CAPABILITY": "synthetic-health-grant-canary",
    "JOBS_FIRE_CAPABILITY": "synthetic-health-jobs-canary",
}


def scheduler(mode, run_id):
    result = subprocess.run(
        [sys.executable, "-P", str(Path(__file__).with_name("_health_qa_scheduler.py"))],
        env=os.environ
        | {
            "PYTHONPATH": "/app/scheduler:/app",
            "API_BASE_URL": os.environ["TEST_API_BASE_URL"],
            "HEALTH_QA_MODE": mode,
            "HEALTH_QA_RUN": run_id,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def health_qa(real_redis):
    api = LanggraphAPIClient()
    api.base_url = os.environ["TEST_API_BASE_URL"]
    stream = RedisStreamClient(os.environ["REDIS_URL"])
    await stream.connect()
    prior = await real_redis.get(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
    now = datetime.now(UTC)
    snapshot = ExecutorDiagnosticSnapshot(
        schema_version="v2",
        version="health-qa-service-no-model",
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
        diagnostics=[
            ExecutorDiagnostic(
                executor=executor,
                enabled=True,
                auth_mode=ExecutorAuthMode.HOST_SESSION,
                availability=ExecutorAvailability.UNAVAILABLE,
                observed_at=now,
                expires_at=now + timedelta(minutes=5),
                active_lease_count=0,
                reason_code="profile_logged_out",
                reason="Host session is absent.",
                profile=host_profile_for_reason("profile_logged_out"),
            )
            for executor in (AgentType.CLAUDE, AgentType.CODEX)
        ],
    )
    await real_redis.set(EXECUTOR_DIAGNOSTICS_REDIS_KEY, snapshot.model_dump_json(), ex=300)
    try:
        telegram = uuid.uuid4().int % 1_000_000_000
        owner = await api.post(
            "users/", json={"telegram_id": telegram, "username": f"health-{telegram}"}
        )
        project = await api.post(
            "projects/",
            headers={"X-Telegram-ID": str(telegram)},
            json={"title": "Health QA", "status": "active", "initiating_run_id": "health-init"},
        )
        pid = project["id"]
        await api.post(f"projects/{pid}/config/secrets", json={"secrets": CAPABILITIES})
        await api.request(
            "PUT",
            f"engineering-budget-policies/{owner['id']}",
            json={"limit_microusd": 100, "attempt_reservation_microusd": 60, "state": "enabled"},
        )
        server = await api.post(
            "servers/",
            json={
                "handle": f"health-{uuid.uuid4().hex[:8]}",
                "host": "fixture.test",
                "public_ip": "10.9.0.9",
                "ssh_key": fleet_private_key(),
            },
        )
        repo = await api.post(
            "repositories/",
            json={
                "project_id": pid,
                "name": f"health-{uuid.uuid4().hex[:8]}",
                "git_url": "https://github.com/synthetic/health.git",
                "role": "primary",
            },
        )
        app = await api.post(
            "applications/",
            json={
                "repo_id": repo["id"],
                "server_handle": server["handle"],
                "service_name": "health-fixture",
                "status": "running",
            },
        )
        story = await api.post("stories/", json={"project_id": pid, "title": "Health QA"})
        for action in ("start", "deploy"):
            await api.transition_story(story["id"], action)
        yield api, stream, project, story, app, owner
    finally:
        if prior is None:
            await real_redis.delete(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
        else:
            await real_redis.set(EXECUTOR_DIAGNOSTICS_REDIS_KEY, prior, ex=300)
        await stream.close()
        await api.close()


async def admit_and_publish(health_qa, real_redis):
    api, _, project, story, app, owner = health_qa
    run_id = f"qa-health-chain-{uuid.uuid4().hex}"
    plan = QAHandoffPlan(
        qa_message=QAMessage(
            project_id=project["id"],
            story_id=story["id"],
            initiating_run_id="health-init",
            run_id=run_id,
            application_id=app["id"],
            deployed_url=PRODUCT,
            acceptance_criteria="- GET /health returns 200",
            telegram_chat_id=str(owner["telegram_id"]),
        )
    )
    command = PaidRunStartCommand(
        id=run_id,
        type="qa",
        project_id=project["id"],
        story_id=story["id"],
        run_metadata={QA_HANDOFF_KEY: plan.model_dump(mode="json"), "application_id": app["id"]},
    )
    started = await api.post("work-admission/paid-runs", json=command.model_dump(mode="json"))
    assert started["admission"]["outcome"] == "admitted"
    queued = await api.get(f"runs/{run_id}")
    assert queued["status"] == "queued" and queued["result"] is None
    assert queued["run_metadata"][QA_HANDOFF_KEY] == plan.model_dump(mode="json")
    assert queued["run_metadata"]["executor_decision"] == started["executor_decision"]
    hold = await api.get(f"engineering-budget-policies/admissions/{run_id}")
    assert hold["reservation_state"] == "active" and hold["active_held_microusd"] == 60
    await api.transition_story(story["id"], "test")
    scheduler("publish", run_id)
    dispatched = await api.get(f"runs/{run_id}")
    assert dispatched["run_metadata"]["qa_dispatched_at"]
    messages = [json.loads(fields[b"data"]) for _, fields in await real_redis.xrange(QA_QUEUE)]
    message = next(item for item in messages if item["run_id"] == run_id)
    assert QAMessage.model_validate(message) == plan.qa_message
    return run_id, plan, message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome", "story_status"),
    [
        (200, "passed", "completed"),
        (503, "failed", "in_progress"),
        ("refused", "blocked", "waiting_human_review"),
    ],
    ids=["healthy", "unhealthy", "refused"],
)
async def test_admitted_health_qa_persists_and_routes_without_a_model(
    health_qa, real_redis, response, outcome, story_status
):
    api, stream, project, story, _, _ = health_qa
    assert get_settings().llm_codex_home is None
    assert get_settings().claude_code_oauth_token is None
    run_id, plan, message = await admit_and_publish(health_qa, real_redis)
    commands_before = await real_redis.xlen(WORKER_COMMANDS)

    def product_read(request):
        assert request.headers["X-Identity-Capability"] == CAPABILITIES["USER_IDENTITY_CAPABILITY"]
        assert request.headers["X-User-Channel"] == "qa"
        assert request.headers["X-User-External-Id"] == "central-qa"
        if response == "refused":
            raise httpx.ConnectError("controlled product refusal", request=request)
        return httpx.Response(response, text=" ".join(CAPABILITIES.values()))

    with (
        respx.mock(assert_all_called=True) as transport,
        patch("src.consumers.qa.api_client", api),
        patch("src.consumers._qa_runner.HEALTH_CHECK_RETRY_DELAY", 0),
        patch(
            "src.consumers.qa.run_qa_centrally", AsyncMock(side_effect=AssertionError("paid QA"))
        ) as paid,
        patch(
            "src.consumers._qa_runner.run_qa_executor",
            AsyncMock(side_effect=AssertionError("model")),
        ) as executor,
    ):
        transport.route(host="control-api").pass_through()
        transport.get(PRODUCT).respond(200)
        grant = transport.post(f"{PRODUCT}/users/grant").respond(200)
        transport.get(f"{PRODUCT}/users/access").respond(
            200, json={"channel": "qa", "external_id": "central-qa", "status": "active"}
        )
        read = transport.get(f"{PRODUCT}/health").mock(side_effect=product_read)
        await process_qa_job(message, stream)
        paid.assert_not_awaited()
        executor.assert_not_awaited()
        assert read.called
        assert json.loads(grant.calls.last.request.content) == {
            "channel": "qa",
            "external_id": "central-qa",
        }
    assert await real_redis.xlen(WORKER_COMMANDS) == commands_before
    terminal = await api.get(f"runs/{run_id}")
    # A missing consumer terminal write must fail, even when the product answered.
    assert terminal["status"] == "completed" and terminal["completed_at"] is not None
    assert terminal["result"]["qa_outcome"] == outcome
    assert terminal["run_metadata"]["qa_caller_identity"] == {
        "user_ref": "qa:central-qa",
        "active": True,
    }
    assert terminal["run_metadata"][QA_HANDOFF_KEY] == plan.model_dump(mode="json")
    assert terminal["qa_routed_at"] is None
    assert all(value not in json.dumps(terminal) for value in CAPABILITIES.values())
    if outcome == "failed":
        detail = terminal["result"]["failed_checks"][0]["detail"]
        assert "got 503, expected 200" in detail and "[redacted:" in detail
    if outcome == "blocked":
        assert terminal["result"]["blocker"]["category"] == "deployed_url_unreachable"
    balance = await api.get(f"engineering-budget-policies/{project['owner_id']}/balance")
    assert balance["known_spend_microusd"] == balance["active_held_microusd"] == 0
    assert balance["unknown_cost_attempt_count"] == 0 and balance["available_microusd"] == 100
    released = await api.get(f"engineering-budget-policies/admissions/{run_id}")
    assert released["reservation_state"] == "released"
    assert released["active_held_microusd"] == 0
    async with await AsyncConnection.connect(os.environ["TEST_DATABASE_URL"]) as db:
        row = await (
            await db.execute(
                "SELECT count(*) FROM engineering_attempt_ledger WHERE run_id=%s", (run_id,)
            )
        ).fetchone()
        assert row == (0,)
    scheduler("route", run_id)
    routed = await api.get(f"runs/{run_id}")
    final_story = await api.get(f"stories/{story['id']}")
    assert routed["qa_routed_at"] is not None
    assert routed["result"] == terminal["result"]
    assert final_story["status"] == story_status
    if outcome == "blocked":
        assert final_story["quarantine_reason"]["blocker"]["category"] == "deployed_url_unreachable"
    if outcome == "failed":
        tasks = await api.get("tasks/", params={"story_id": story["id"]})
        assert len(tasks) == 1 and tasks[0]["failure_metadata"]["qa_failure"]["qa_run_id"] == run_id
    assert await real_redis.xlen(WORKER_COMMANDS) == commands_before
