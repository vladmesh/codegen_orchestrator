"""Unit tests for the admin console read models (journeys, passports, attention)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.executor_decision import EXECUTOR_DECISION_METADATA_KEY
from src.admin_console import (
    attention_items,
    build_passports,
    build_steps,
    median_lead_time_minutes,
    stage_for,
)
from src.main import app
from src.schemas.admin_console import (
    AttentionResponse,
    ConsoleKpis,
    JourneyStage,
    Severity,
    StepStatus,
    TopologyResponse,
)

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def story(**overrides):
    base = {
        "id": "st-1",
        "project_id": PROJECT,
        "title": "Weather posts",
        "status": "completed",
        "waiting_on": "none",
        "created_at": at(0),
        "updated_at": at(26),
        "status_entered_at": at(26),
        "pr_number": 14,
        "unverified_decisions": [],
    }
    return SimpleNamespace(**{**base, **overrides})


def task(task_id, *, type="feature", status="done", minutes=5, **overrides):
    base = {
        "id": task_id,
        "project_id": PROJECT,
        "story_id": "st-1",
        "type": type,
        "status": status,
        "title": task_id,
        "created_at": at(minutes),
        "updated_at": at(minutes + 1),
        "install": None,
        "install_operation": None,
        "failure_metadata": None,
    }
    return SimpleNamespace(**{**base, **overrides})


def run(run_id, type, status, start, end, **overrides):
    base = {
        "id": run_id,
        "type": type,
        "status": status,
        "task_id": None,
        "run_metadata": {},
        "started_at": at(start),
        "created_at": at(start),
        "completed_at": at(end) if end is not None else None,
        "error_message": None,
        "result": None,
    }
    return SimpleNamespace(**{**base, **overrides})


BRIEF = SimpleNamespace(id="pb-1", revision=3, created_at=at(1), confirmed_at=at(4))
INSTALL = {
    "package": {"name": "reminders", "version": "1.3.0"},
    "libraries": [{"name": "textparse", "version": "0.1.0"}],
}


def by_stage(steps):
    return {s.stage: s for s in steps}


def test_completed_story_walks_every_stage_and_keeps_the_retry_visible():
    steps = by_stage(
        build_steps(
            story(),
            BRIEF,
            [
                task(
                    "t-install",
                    type="install",
                    install=INSTALL,
                    install_operation={"state": "published"},
                ),
                task("t-1"),
            ],
            [
                run("eng-1", "engineering", "failed", 7, 10, error_message="ruff failed"),
                run("eng-2", "engineering", "completed", 11, 15),
                run("dep-1", "deploy", "completed", 19, 22, result={"deployed_url": "http://x"}),
                run("qa-1", "qa", "completed", 22, 25),
            ],
        )
    )

    assert [s.status for s in steps.values()] == [StepStatus.DONE] * 8
    build = steps[JourneyStage.BUILD]
    assert [a.status for a in build.attempts] == ["failed", "completed"]
    assert build.attempts[0].error == "ruff failed"
    assert (build.started_at, build.finished_at) == (at(7), at(15))
    assert {f.value for f in steps[JourneyStage.INSTALL].facts} == {
        "reminders 1.3.0",
        "textparse 0.1.0",
    }
    assert steps[JourneyStage.REVIEW].started_at == at(15)
    assert steps[JourneyStage.REVIEW].finished_at == at(19)
    assert steps[JourneyStage.LIVE].finished_at == at(26)


def test_a_story_without_brief_or_install_marks_those_stages_skipped():
    steps = by_stage(
        build_steps(
            story(),
            None,
            [task("t-1")],
            [run("eng-1", "engineering", "completed", 7, 10)],
        )
    )
    assert steps[JourneyStage.BRIEF].status is StepStatus.SKIPPED
    assert steps[JourneyStage.INSTALL].status is StepStatus.SKIPPED
    assert steps[JourneyStage.INSTALL].started_at is None


def test_waiting_story_parks_at_its_waiting_stage_and_leaves_the_rest_pending():
    steps = by_stage(
        build_steps(
            story(status="in_progress", waiting_on="human_review", status_entered_at=at(9)),
            BRIEF,
            [task("t-1", status="waiting_human_review")],
            [run("eng-1", "engineering", "failed", 7, 9)],
        )
    )
    assert steps[JourneyStage.BUILD].status is StepStatus.WAITING
    assert steps[JourneyStage.BUILD].finished_at is None
    assert steps[JourneyStage.REVIEW].status is StepStatus.PENDING
    assert steps[JourneyStage.LIVE].status is StepStatus.PENDING


def test_failed_story_stops_at_its_last_attempted_stage():
    steps = by_stage(
        build_steps(
            story(status="failed"),
            BRIEF,
            [task("t-1")],
            [
                run("eng-1", "engineering", "completed", 7, 10),
                run("dep-1", "deploy", "failed", 19, 22, error_message="smoke failed"),
            ],
        )
    )
    assert steps[JourneyStage.BUILD].status is StepStatus.DONE
    assert steps[JourneyStage.DEPLOY].status is StepStatus.FAILED
    assert steps[JourneyStage.VERIFY].status is StepStatus.SKIPPED
    assert steps[JourneyStage.LIVE].status is StepStatus.SKIPPED


def test_running_attempt_keeps_its_step_open():
    steps = by_stage(
        build_steps(
            story(status="in_progress"),
            BRIEF,
            [task("t-1", status="in_dev")],
            [
                run(
                    "eng-1",
                    "engineering",
                    "running",
                    7,
                    None,
                    run_metadata={
                        EXECUTOR_DECISION_METADATA_KEY: {
                            "attempt_kind": "engineering",
                            "agent_type": "claude",
                            "source": "api_default",
                            "policy_version": "v2",
                            "reason": "default executor",
                        }
                    },
                )
            ],
        )
    )
    build = steps[JourneyStage.BUILD]
    assert build.status is StepStatus.ACTIVE
    assert build.finished_at is None
    assert build.attempts[0].actor == "claude"


def test_stage_for_prefers_what_the_story_waits_on():
    assert stage_for("deploying", "none") is JourneyStage.DEPLOY
    assert stage_for("in_progress", "ci") is JourneyStage.REVIEW
    assert stage_for("failed", "none") is None


def test_passport_lists_published_packages_container_ports_and_secret_names_only():
    project = SimpleNamespace(
        id=PROJECT,
        config={
            "modules": ["backend", "tg_bot"],
            "secrets": {"OPENWEATHER_KEY": "ciphertext", "TELEGRAM_TOKEN": "ciphertext"},
        },
    )
    published = task(
        "t-a", type="install", install=INSTALL, install_operation={"state": "published"}
    )
    refused = task(
        "t-b",
        type="install",
        install={"package": {"name": "other", "version": "0.1.0"}},
        install_operation={"state": "refused"},
    )
    repo = SimpleNamespace(id="repo-1", project_id=PROJECT)
    app_row = SimpleNamespace(
        id=7,
        repo_id="repo-1",
        service_name="backend",
        status="running",
        server_handle="vps-fra-1",
        reserved_ram_mb=384,
        response_time_ms=41,
        uptime_pct_24h=100.0,
    )
    port = SimpleNamespace(application_id=7, port=8102)
    deployments = [
        SimpleNamespace(application_id=7, deployed_sha="old", deployed_at=at(0)),
        SimpleNamespace(application_id=7, deployed_sha="a91f3c2", deployed_at=at(20)),
    ]

    passport = build_passports(
        [project], [published, refused], [repo], [app_row], [port], deployments
    )[PROJECT]

    assert passport.modules == ["backend", "tg_bot"]
    assert [(p.name, p.kind) for p in passport.packages] == [
        ("textparse", "library"),
        ("reminders", "package"),
    ]
    container = passport.containers[0]
    assert (container.placement, container.port, container.deployed_sha) == (
        "vps-fra-1",
        8102,
        "a91f3c2",
    )
    assert passport.user_secrets == ["OPENWEATHER_KEY", "TELEGRAM_TOKEN"]
    assert "ciphertext" not in passport.model_dump_json()


def test_attention_puts_critical_first_then_most_recent():
    titles = {PROJECT: "weather-cats"}
    tasks = [
        task(
            "t-wait",
            status="waiting_resources",
            updated_at=at(30),
        ),
        task(
            "t-human",
            status="waiting_human_review",
            updated_at=at(10),
            failure_metadata={"reason": "channel not found"},
        ),
    ]
    stories = [story(status="in_progress", waiting_on="user_secret", status_entered_at=at(20))]
    incidents = [
        SimpleNamespace(
            incident_type="service_down",
            status="detected",
            detected_at=at(5).replace(tzinfo=None),
            affected_services=["backend"],
            server_handle="vps-ams-2",
        )
    ]
    app_row = SimpleNamespace(
        id=3,
        service_name="backend",
        status="degraded",
        response_time_ms=1800,
        last_health_check=at(25),
        updated_at=at(25),
        server_handle="vps-ams-2",
    )

    items = attention_items(
        titles, tasks, stories, incidents, [(app_row, PROJECT)], ["engineering:queue lag"]
    )

    assert [(i.kind, i.severity) for i in items] == [
        ("task", Severity.CRITICAL),
        ("incident", Severity.CRITICAL),
        ("task", Severity.WARNING),
        ("application", Severity.WARNING),
        ("story", Severity.WARNING),
        ("queue", Severity.WARNING),
    ]
    assert items[0].detail == "channel not found"
    assert items[0].project_title == "weather-cats"


def test_median_lead_time_ignores_stories_without_a_completion_stamp():
    stories = [
        story(created_at=at(0), status_entered_at=at(20)),
        story(created_at=at(0), status_entered_at=at(30)),
        story(created_at=at(0), status_entered_at=at(40)),
        story(status_entered_at=None),
    ]
    assert median_lead_time_minutes(stories) == 30.0
    assert median_lead_time_minutes([]) is None


@pytest.mark.asyncio
async def test_console_routes_are_internal_or_admin_only():
    attention = AttentionResponse(
        kpis=ConsoleKpis(
            active_journeys=0,
            running_runs=0,
            queued_runs=0,
            live_products=0,
            degraded_containers=0,
            median_lead_time_minutes_7d=None,
        ),
        items=[],
    )
    topology = TopologyResponse(placements=[], products=[], platform_services=[])
    with (
        patch("src.admin_console.build_attention", new=AsyncMock(return_value=attention)),
        patch("src.admin_console.build_topology", new=AsyncMock(return_value=topology)),
        patch("src.admin_console.list_journeys", new=AsyncMock(return_value=[])),
        patch("src.admin_console.load_journey", new=AsyncMock(return_value=None)),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("attention", "topology", "journeys", "journeys/st-1"):
                anonymous = await client.get(f"/api/admin/v2/{path}")
                assert anonymous.status_code == 401
            assert (await client.get("/api/admin/v2/attention", headers=INTERNAL_HEADERS)).json()[
                "items"
            ] == []
            assert (await client.get("/api/admin/v2/topology", headers=INTERNAL_HEADERS)).json()[
                "platform_services"
            ] == []
            assert (
                await client.get("/api/admin/v2/journeys", headers=INTERNAL_HEADERS)
            ).json() == []
            missing = await client.get("/api/admin/v2/journeys/st-1", headers=INTERNAL_HEADERS)
            assert missing.status_code == 404
