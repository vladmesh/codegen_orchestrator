"""Unit tests for the admin console read models (journeys, passports, attention)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
from pydantic import ValidationError
import pytest

from shared.contracts.dto.executor_decision import EXECUTOR_DECISION_METADATA_KEY
from shared.contracts.dto.task import TaskStatus
from src.admin_console import (
    attention_items,
    build_passports,
    build_steps,
    build_task_board,
    median_lead_time_minutes,
    platform_service_views,
    scope_attention,
    stage_for,
)
from src.config import Settings
from src.main import app
from src.schemas.admin_console import (
    AttentionResponse,
    ConsoleKpis,
    JourneyStage,
    Severity,
    StepStatus,
    StoryLinkCreate,
    TaskBoardColumn,
    TaskOrigin,
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


CLAUDE_DECISION = {
    EXECUTOR_DECISION_METADATA_KEY: {
        "attempt_kind": "engineering",
        "agent_type": "claude",
        "source": "api_default",
        "policy_version": "v2",
        "reason": "default executor",
    }
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


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (None, StepStatus.ACTIVE),
        ({"state": "running", "stage": "preflight"}, StepStatus.ACTIVE),
        ({"state": "refused"}, StepStatus.FAILED),
    ],
)
def test_an_install_under_way_stays_active_while_the_story_is_building(operation, expected):
    steps = by_stage(
        build_steps(
            story(status="in_progress", pr_number=None),
            BRIEF,
            [
                task(
                    "t-install",
                    type="install",
                    status="todo",
                    install=INSTALL,
                    install_operation=operation,
                ),
                task("t-1", status="todo"),
            ],
            [],
        )
    )

    install = steps[JourneyStage.INSTALL]
    assert install.status is expected
    assert install.attempts[0].status == (operation or {}).get("state", "queued")
    assert steps[JourneyStage.BUILD].status is StepStatus.ACTIVE


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
                    run_metadata=CLAUDE_DECISION,
                )
            ],
        )
    )
    build = steps[JourneyStage.BUILD]
    assert build.status is StepStatus.ACTIVE
    assert build.finished_at is None
    assert build.attempts[0].actor == "claude"


def test_every_task_status_has_its_board_column():
    expected = {
        TaskStatus.BACKLOG: TaskBoardColumn.BACKLOG,
        TaskStatus.TODO: TaskBoardColumn.BACKLOG,
        TaskStatus.IN_DEV: TaskBoardColumn.IN_DEVELOPMENT,
        TaskStatus.BLOCKED: TaskBoardColumn.IN_DEVELOPMENT,
        TaskStatus.WAITING_HUMAN_REVIEW: TaskBoardColumn.IN_DEVELOPMENT,
        TaskStatus.WAITING_RESOURCES: TaskBoardColumn.IN_DEVELOPMENT,
        TaskStatus.IN_CI: TaskBoardColumn.CI,
        TaskStatus.TESTING: TaskBoardColumn.CI,
        TaskStatus.DONE: TaskBoardColumn.DONE,
        TaskStatus.FAILED: TaskBoardColumn.DONE,
        TaskStatus.CANCELLED: TaskBoardColumn.DONE,
    }
    assert set(expected) == set(TaskStatus)

    statuses = list(TaskStatus)
    board = build_task_board(
        [task(f"t-{s}", status=s.value, minutes=i) for i, s in enumerate(statuses)], []
    )

    assert [(e.status, e.column) for e in board] == [(s, expected[s]) for s in statuses]


def test_a_fix_task_names_the_failed_run_kind_that_created_it():
    board = build_task_board(
        [
            task("t-qa-fix", type="fix", minutes=3, failure_metadata={"qa_failure": {"run": "qa"}}),
            task("t-ci-fix", type="fix", minutes=2, failure_metadata={"ci_failure": {"job": "x"}}),
            task("t-plan", minutes=1, failure_metadata={"reason": "parked once"}),
        ],
        [],
    )

    assert [(e.task_id, e.origin) for e in board] == [
        ("t-plan", TaskOrigin.PLANNED),
        ("t-ci-fix", TaskOrigin.CI_FIX),
        ("t-qa-fix", TaskOrigin.QA_FIX),
    ]


def test_task_attempts_are_its_engineering_runs_numbered_oldest_first_with_their_workers():
    board = build_task_board(
        [task("t-1", status="in_ci")],
        [
            run(
                "eng-2",
                "engineering",
                "completed",
                11,
                15,
                task_id="t-1",
                run_metadata={**CLAUDE_DECISION, "worker_id": "w-2"},
            ),
            run("dep-1", "deploy", "completed", 19, 22, task_id="t-1"),
            run("eng-other", "engineering", "completed", 1, 2, task_id="t-other"),
            run(
                "eng-1",
                "engineering",
                "failed",
                7,
                10,
                task_id="t-1",
                run_metadata={"worker_id": "w-1"},
                started_at=at(7).replace(tzinfo=None),
            ),
        ],
    )

    [entry] = board
    assert [(a.number, a.run_id, a.status, a.worker_id, a.actor) for a in entry.attempts] == [
        (1, "eng-1", "failed", "w-1", None),
        (2, "eng-2", "completed", "w-2", "claude"),
    ]
    assert (entry.worker_id, entry.attempt_count) == ("w-2", 2)
    first = entry.model_dump(mode="json")["attempts"][0]
    assert (first["started_at"], first["finished_at"]) == (
        "2026-10-07T14:07:00Z",
        "2026-10-07T14:10:00Z",
    )


def test_a_planned_task_without_an_attempt_has_no_worker():
    [entry] = build_task_board([task("t-1", status="todo")], [])

    assert entry.origin is TaskOrigin.PLANNED
    assert (entry.attempts, entry.worker_id, entry.attempt_count) == ([], None, 0)


def test_a_run_with_no_worker_recorded_yet_has_a_null_worker():
    [entry] = build_task_board(
        [task("t-1", status="in_dev")],
        [
            run(
                "eng-1",
                "engineering",
                "running",
                7,
                None,
                task_id="t-1",
                run_metadata=CLAUDE_DECISION,
            )
        ],
    )

    [attempt] = entry.attempts
    assert (attempt.worker_id, attempt.actor, attempt.finished_at) == (None, "claude", None)
    assert (entry.worker_id, entry.attempt_count) == (None, 1)


def test_a_damaged_worker_record_on_a_run_raises():
    with pytest.raises(ValidationError):
        build_task_board(
            [task("t-1", status="in_dev")],
            [
                run(
                    "eng-1",
                    "engineering",
                    "running",
                    7,
                    None,
                    task_id="t-1",
                    run_metadata={"worker_id": 7},
                )
            ],
        )


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
        titles, tasks, stories, incidents, [(app_row, PROJECT)], ["engineering:queue lag"], []
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


def test_attention_scope_keeps_one_project_and_recent_items():
    other = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    tasks = [
        task("t-old", status="waiting_human_review", updated_at=at(-600)),
        task("t-new", status="waiting_human_review", updated_at=at(10)),
        task("t-other", status="waiting_human_review", updated_at=at(10), project_id=other),
    ]
    incidents = [
        SimpleNamespace(
            incident_type="service_down",
            status="detected",
            detected_at=at(20),
            affected_services=[],
            server_handle="vps-ams-2",
        )
    ]
    items = attention_items({}, tasks, [], incidents, [], ["engineering:queue lag"], [])

    recent = scope_attention(items, at(0), None)
    assert sorted(i.task_id or i.kind for i in recent) == ["incident", "queue", "t-new", "t-other"]

    scoped = scope_attention(items, at(0), PROJECT)
    assert [i.task_id for i in scoped] == ["t-new"]
    assert scope_attention(items, None, None) == items


def ci_run(run_id, conclusion, start=None, end=None, jobs=()):
    return {
        "id": run_id,
        "url": f"https://github.com/org/repo/actions/runs/{run_id}",
        "status": "completed",
        "conclusion": conclusion,
        "failed_jobs": [{"name": name, "failed_steps": []} for name in jobs],
        **({"started_at": at(start).isoformat()} if start is not None else {}),
        **({"completed_at": at(end).isoformat()} if end is not None else {}),
    }


def ci_story(*runs, **overrides):
    return story(
        **{
            "status": "pr_review",
            "generated_product_timeline": {"ci_runs": list(runs)},
            **overrides,
        }
    )


def test_attention_names_the_failed_jobs_of_a_story_whose_latest_ci_failed():
    stories = [
        ci_story(ci_run(2, "failure", 40, 47, ["lint", "unit"]), ci_run(1, "success", 30, 35))
    ]

    [item] = attention_items({PROJECT: "weather-cats"}, [], [], [], [], [], stories)

    assert item.kind == "ci"
    assert item.severity == Severity.WARNING
    assert item.title == "CI failed on PR #14: Weather posts"
    assert item.detail == "lint, unit"
    assert item.since == at(47)
    assert (item.story_id, item.project_id, item.project_title) == ("st-1", PROJECT, "weather-cats")


def test_attention_drops_a_ci_failure_once_a_later_run_concludes_otherwise():
    green = ci_story(ci_run(1, "failure", 10, 15, ["unit"]), ci_run(2, "success", 20, 25))
    pending = ci_story(ci_run(1, "failure", 10, 15, ["unit"]), ci_run(2, None, 20))
    done = ci_story(ci_run(1, "failure", 10, 15, ["unit"]), status="completed")

    assert attention_items({}, [], [], [], [], [], [green, pending, done]) == []


def test_attention_orders_untimed_ci_runs_by_position_and_dates_them_by_the_story():
    failing = ci_story(ci_run(1, "success"), ci_run(2, "failure", jobs=["e2e"]), updated_at=at(50))
    recovered = ci_story(ci_run(1, "failure", jobs=["e2e"]), ci_run(2, "success"))

    [item] = attention_items({}, [], [], [], [], [], [failing, recovered])

    assert (item.detail, item.since) == ("e2e", at(50))


def test_attention_scope_keeps_the_ci_items_of_one_project():
    other = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    stories = [
        ci_story(ci_run(1, "failure", 10, 15, ["unit"])),
        ci_story(ci_run(1, "failure", 10, 15, ["unit"]), id="st-2", project_id=other),
    ]
    items = attention_items({}, [], [], [], [], [], stories)

    assert [i.story_id for i in scope_attention(items, None, other)] == ["st-2"]


@pytest.mark.parametrize("ci_runs", ["failure", [["failure"]], None])
def test_attention_refuses_malformed_ci_runs(ci_runs):
    malformed = story(status="pr_review", generated_product_timeline={"ci_runs": ci_runs})

    with pytest.raises(ValueError, match="ci_runs"):
        attention_items({}, [], [], [], [], [], [malformed])


def test_platform_services_join_declared_and_used_with_their_products():
    other = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    views = platform_service_views(
        {"feeds": "https://platform.example/feeds"},
        {"feeds": "up"},
        {PROJECT: ["feeds"], other: ["feeds", "weather"]},
    )
    assert [(v.name, v.status, v.products) for v in views] == [
        ("feeds", "up", [PROJECT, other]),
        ("weather", "undeclared", [other]),
    ]
    assert platform_service_views({}, {}, {}) == []


def test_platform_services_setting_parses_name_url_pairs():
    settings = SimpleNamespace(platform_services=" feeds=https://p.example/feeds, ,bad, ")
    assert Settings.get_platform_services(settings) == {"feeds": "https://p.example/feeds"}


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


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/vladmesh/codegen-product-kit/pull/42",
        "https://github.com/owner/repo.name/pull/7",
    ],
)
def test_a_linked_pull_request_accepts_a_github_pull_url(url):
    link = StoryLinkCreate(kind="module_proposal", title="Reminders module", url=url)
    assert link.url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/vladmesh/codegen-product-kit/pull/42",
        "https://gitlab.com/vladmesh/codegen-product-kit/pull/42",
        "https://github.com.evil.test/vladmesh/kit/pull/42",
        "https://github.com/vladmesh/codegen-product-kit",
        "https://github.com/vladmesh/codegen-product-kit/pull/42/files",
        "javascript:alert(1)",
    ],
)
def test_a_linked_pull_request_refuses_anything_but_a_github_pull_url(url):
    with pytest.raises(ValidationError):
        StoryLinkCreate(kind="module_proposal", title="Reminders module", url=url)


def test_a_linked_pull_request_refuses_an_unknown_kind():
    with pytest.raises(ValidationError):
        StoryLinkCreate(kind="anything", title="x", url="https://github.com/vladmesh/kit/pull/1")


@pytest.mark.asyncio
async def test_link_routes_are_internal_or_admin_only():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        added = await client.post(
            "/api/admin/v2/journeys/st-1/links",
            json={
                "kind": "module_proposal",
                "title": "Reminders module",
                "url": "https://github.com/vladmesh/codegen-product-kit/pull/42",
            },
        )
        assert added.status_code == 401
        removed = await client.delete("/api/admin/v2/journeys/st-1/links/1")
        assert removed.status_code == 401
