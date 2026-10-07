"""The admin console read models assemble one product's full path from real rows."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest

from shared.models import (
    Application,
    Deployment,
    PortAllocation,
    ProductBrief,
    Project,
    Repository,
    Run,
    Server,
    Story,
    Task,
    User,
)

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
async def product(db_session):
    """One completed story of a deployed product plus one parked task elsewhere."""
    suffix = uuid.uuid4().hex[:8]
    owner = User(telegram_id=int(suffix, 16), username=f"owner_{suffix}")
    db_session.add(owner)
    await db_session.flush()
    owner_id = owner.id
    project = Project(
        id=uuid.uuid4(),
        title=f"weather-cats-{suffix}",
        slug=f"wc-{suffix}",
        status="active",
        initiating_run_id=f"run-init-{suffix}",
        owner_id=owner_id,
        config={"modules": ["backend", "tg_bot"], "secrets": {"OPENWEATHER_KEY": "enc"}},
    )
    server = Server(handle=f"vps-{suffix}", host=f"{suffix}.test", public_ip="10.9.0.1")
    repo = Repository(
        id=f"repo-{suffix}",
        project_id=project.id,
        name=f"wc-{suffix}",
        git_url=f"https://github.com/test/wc-{suffix}.git",
    )
    story = Story(
        id=f"st-{suffix}",
        project_id=project.id,
        title="Weather channel posts",
        status="completed",
        waiting_on="none",
        pr_number=14,
        created_at=at(0),
        status_entered_at=at(26),
    )
    parked = Story(
        id=f"st-parked-{suffix}",
        project_id=project.id,
        title="Cats",
        status="in_progress",
        waiting_on="human_review",
    )
    db_session.add_all([project, server])
    await db_session.flush()
    db_session.add_all([repo, story, parked])
    await db_session.flush()
    brief = ProductBrief(
        id=f"pb-{suffix}",
        project_id=project.id,
        story_id=story.id,
        revision=3,
        title="Weather channel posts",
        content={"summary": "Add channel posts to the forecast", "must_requirements": [{}, {}]},
        request_id=f"req-{suffix}",
        created_at=at(1),
        confirmed_at=at(4),
    )
    install = Task(
        id=f"t-install-{suffix}",
        project_id=project.id,
        story_id=story.id,
        repository_id=repo.id,
        type="install",
        title="Install reminders",
        status="done",
        created_at=at(5),
        install={
            "package": {"name": "reminders", "version": "1.3.0"},
            "libraries": [{"name": "textparse", "version": "0.1.0"}],
        },
        install_operation={"state": "published"},
    )
    feature = Task(
        id=f"t-feature-{suffix}",
        project_id=project.id,
        story_id=story.id,
        repository_id=repo.id,
        type="feature",
        title="Posts",
        status="done",
        created_at=at(5),
    )
    waiting = Task(
        id=f"t-wait-{suffix}",
        project_id=project.id,
        story_id=parked.id,
        repository_id=repo.id,
        type="feature",
        title="Cats",
        status="waiting_human_review",
        failure_metadata={"reason": "no cat API key"},
    )
    runs = [
        Run(
            id=f"eng-1-{suffix}",
            type="engineering",
            status="failed",
            project_id=project.id,
            story_id=story.id,
            task_id=feature.id,
            started_at=at(7),
            completed_at=at(10),
            error_message="ruff failed",
        ),
        Run(
            id=f"eng-2-{suffix}",
            type="engineering",
            status="completed",
            project_id=project.id,
            story_id=story.id,
            task_id=feature.id,
            started_at=at(11),
            completed_at=at(15),
        ),
        Run(
            id=f"dep-{suffix}",
            type="deploy",
            status="completed",
            project_id=project.id,
            story_id=story.id,
            started_at=at(19),
            completed_at=at(22),
        ),
    ]
    db_session.add_all([brief, install, feature, waiting])
    await db_session.flush()
    db_session.add_all(runs)
    await db_session.flush()
    app_row = Application(
        repo_id=repo.id,
        server_handle=server.handle,
        service_name="backend",
        status="degraded",
        reserved_ram_mb=384,
    )
    db_session.add(app_row)
    await db_session.flush()
    db_session.add_all(
        [
            PortAllocation(
                server_handle=server.handle,
                port=8102,
                application_id=app_row.id,
                service_name="backend",
            ),
            Deployment(
                application_id=app_row.id,
                project_id=project.id,
                service_name="backend",
                server_handle=server.handle,
                port=8102,
                result="success",
                deployed_sha="a91f3c2",
            ),
        ]
    )
    await db_session.commit()
    return {"project": project, "story": story, "server": server, "waiting": waiting}


async def test_journey_detail_lays_out_the_story_and_its_passport(async_client, product):
    story_id = product["story"].id
    response = await async_client.get(f"/api/admin/v2/journeys/{story_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["request"] == "Add channel posts to the forecast"
    assert body["requirements"] == 2
    steps = {s["stage"]: s for s in body["steps"]}
    # No QA run was recorded, so verification shows as skipped rather than guessed done.
    assert {stage: s["status"] for stage, s in steps.items()} == {
        **dict.fromkeys(steps, "done"),
        "verify": "skipped",
    }
    assert [a["status"] for a in steps["build"]["attempts"]] == ["failed", "completed"]
    assert steps["install"]["facts"] == [
        {"label": "package", "value": "reminders 1.3.0"},
        {"label": "library", "value": "textparse 0.1.0"},
    ]
    passport = body["passport"]
    assert passport["modules"] == ["backend", "tg_bot"]
    assert passport["user_secrets"] == ["OPENWEATHER_KEY"]
    assert passport["containers"] == [
        {
            "name": "backend",
            "status": "degraded",
            "placement": product["server"].handle,
            "port": 8102,
            "reserved_ram_mb": 384,
            "response_time_ms": None,
            "uptime_pct_24h": None,
            "deployed_sha": "a91f3c2",
        }
    ]


async def test_journey_list_filters_by_project(async_client, product):
    project_id = product["project"].id
    response = await async_client.get(f"/api/admin/v2/journeys?project_id={project_id}")
    assert response.status_code == 200
    stages = {j["story_id"]: j["current_stage"] for j in response.json()}
    assert stages == {product["story"].id: "live", product["waiting"].story_id: "build"}


async def test_topology_places_the_product_on_its_server(async_client, product):
    response = await async_client.get("/api/admin/v2/topology")
    assert response.status_code == 200
    body = response.json()
    assert product["server"].handle in {p["handle"] for p in body["placements"]}
    ours = next(p for p in body["products"] if p["project_id"] == str(product["project"].id))
    assert [c["placement"] for c in ours["passport"]["containers"]] == [product["server"].handle]
    assert [p["name"] for p in ours["passport"]["packages"]] == ["textparse", "reminders"]
    assert body["platform_services"] == []


async def test_attention_lists_the_parked_task_and_the_degraded_container(async_client, product):
    response = await async_client.get("/api/admin/v2/attention")
    assert response.status_code == 200
    items = response.json()["items"]
    task_item = next(i for i in items if i["task_id"] == product["waiting"].id)
    assert task_item["severity"] == "critical"
    assert task_item["detail"] == "no cat API key"
    assert task_item["project_title"] == product["project"].title
    assert any(
        i["kind"] == "application" and i["server_handle"] == product["server"].handle for i in items
    )
    assert response.json()["kpis"]["degraded_containers"] >= 1
