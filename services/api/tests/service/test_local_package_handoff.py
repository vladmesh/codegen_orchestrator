"""Real PostgreSQL/Redis: a package-build FEATURE's completed Run pins its commit into one INSTALL.

The terminal Run write is the only creator; every route to the source's `done` requires it.
"""

import asyncio
from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import func, select
from test_local_install import (
    build_body,
    buyer,
    command,
    local_install,
    post,
    product,
)

from shared.contracts.dto.local_install import local_install_task_id
from shared.models import (
    EngineeringAttemptLedger,
    EngineeringBudgetReservation,
    Run,
    Story,
    Task,
    TaskEvent,
)
from shared.models.commit_recovery import CommitRecovery
from shared.queues import ENGINEERING_QUEUE, SCAFFOLD_QUEUE, WORKER_COMMANDS

COMMIT = local_install({"project": None, "repository": None})["commit"]
ATTEMPT = {
    "provider": "anthropic",
    "input_tokens": 10,
    "output_tokens": 10,
    "cost_microusd": 1500,
    "cost_source": "provider_reported",
}
QUEUES = (ENGINEERING_QUEUE, WORKER_COMMANDS, SCAFFOLD_QUEUE)
HOLD = 5000
BUDGET = {"limit_microusd": 1_000_000, "attempt_reservation_microusd": HOLD, "state": "enabled"}


def success(commit: str | None = COMMIT) -> dict:
    """The LangGraph producer's completed-Run patch for one engineering success."""
    result = {"engineering_status": "done"}
    if commit is not None:
        result["commit_sha"] = commit
    return {"status": "completed", "result": result, "engineering_attempt": ATTEMPT}


async def fresh(db_session, model, key):
    return await db_session.get(model, key, populate_existing=True)


async def reservation(db_session, run_id) -> EngineeringBudgetReservation:
    return await db_session.scalar(
        select(EngineeringBudgetReservation)
        .where(EngineeringBudgetReservation.attempt_id == run_id)
        .execution_options(populate_existing=True)
    )


async def assert_hold_active(db_session, run_id):
    held = await reservation(db_session, run_id)
    assert (held.state, held.active_held_microusd) == ("active", HOLD)


async def admitted_build(client, db_session, owner=None, **source_changes) -> dict:
    """An admitted, started package-build FEATURE with a direct successor and a running Run.

    The owner's enabled budget policy gives that Run an active hold.
    """
    owner = owner or await product(client, await buyer(client))
    buyer_id = (await client.get(f"/api/projects/{owner['project']}")).json()["owner_id"]
    policy = await client.put(f"/api/engineering-budget-policies/{buyer_id}", json=BUDGET)
    assert policy.status_code in {200, 201}, policy.text
    source = await post(client, "/api/tasks/", build_body(owner, **source_changes), 201)
    successor = await post(
        client,
        "/api/tasks/",
        build_body(
            owner,
            title="Use the digest",
            package_build=None,
            blocked_by_task_id=source["id"],
        ),
        201,
    )
    decision = await post(
        client, "/api/work-admission/engineering-dispatches", {"task_id": source["id"]}
    )
    assert decision["outcome"] == "admitted", decision
    await post(client, f"/api/tasks/{source['id']}/start")
    run = decision["run_id"]
    response = await client.patch(f"/api/runs/{run}", json={"status": "running"})
    assert response.status_code == 200, response.text
    await assert_hold_active(db_session, run)
    return {
        "owner": owner,
        "source": source["id"],
        "successor": successor["id"],
        "run": run,
        "child": local_install_task_id(source["id"]),
    }


async def complete(client, build, body=None, status=200):
    response = await client.patch(f"/api/runs/{build['run']}", json=body or success())
    assert response.status_code == status, response.text
    return response.json()


async def produce_done(client, build):
    """The producer's ordinary source transitions after its completed Run patch."""
    for step in ("in_ci", "testing", "done"):
        response = await client.post(
            f"/api/tasks/{build['source']}/transition",
            params={"to_status": step},
            json={"actor": "engineering-worker"},
        )
        if response.status_code != 200:
            return response
    return response


async def story_tasks(db_session, story_id) -> list[Task]:
    rows = await db_session.scalars(
        select(Task).where(Task.story_id == story_id).execution_options(populate_existing=True)
    )
    return list(rows.all())


async def assert_outcome_settled_once(db_session, build, body=None):
    run = await fresh(db_session, Run, build["run"])
    assert (run.status, run.result) == ("completed", (body or success())["result"])
    ledger = (
        await db_session.scalars(
            select(EngineeringAttemptLedger).where(EngineeringAttemptLedger.run_id == run.id)
        )
    ).all()
    assert [(row.cost_microusd, row.total_tokens) for row in ledger] == [(1500, 20)]
    settled = await reservation(db_session, run.id)
    assert (settled.state, settled.active_held_microusd) == ("settled", 0)
    # The INSTALL is unpaid: it owns no Run, ledger row or budget hold.
    for model, column in [
        (Run, Run.task_id),
        (EngineeringAttemptLedger, EngineeringAttemptLedger.task_id),
        (EngineeringBudgetReservation, EngineeringBudgetReservation.task_id),
    ]:
        count = select(func.count()).select_from(model).where(column == build["child"])
        assert await db_session.scalar(count) == 0


async def event_count(db_session, build) -> int:
    ids = [build["source"], build["successor"], build["child"]]
    count = select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id.in_(ids))
    return await db_session.scalar(count)


async def assert_no_handoff(db_session, build):
    assert await fresh(db_session, Task, build["child"]) is None
    successor = await fresh(db_session, Task, build["successor"])
    assert successor.blocked_by_task_id == build["source"]


@pytest.mark.asyncio
async def test_a_completed_build_pins_one_install_before_its_source_and_successor_move(
    async_client, db_session, redis_client
):
    owner = await product(async_client, await buyer(async_client))
    unrelated = await post(async_client, "/api/tasks/", build_body(owner, package_build=None), 201)
    build = await admitted_build(async_client, db_session, owner)
    other = await post(
        async_client,
        "/api/tasks/",
        build_body(owner, package_build=None, blocked_by_task_id=unrelated["id"]),
        201,
    )
    source = await fresh(db_session, Task, build["source"])
    source.planning_attempt_id = "plan-attempt-synthetic"
    await db_session.commit()
    before = {queue: await redis_client.xlen(queue) for queue in QUEUES}

    await complete(async_client, build)

    child = await fresh(db_session, Task, build["child"])
    assert child.install == local_install(owner)
    assert (child.type, child.status, child.blocked_by_task_id) == (
        "install",
        "todo",
        build["source"],
    )
    assert (child.project_id, child.story_id, child.repository_id) == (
        uuid.UUID(owner["project"]),
        owner["story"],
        owner["repository"],
    )
    assert (child.dispatch_admitted, child.planning_attempt_id) == (True, "plan-attempt-synthetic")
    assert (child.install_operation, child.package_build) == (None, None)
    successor = await fresh(db_session, Task, build["successor"])
    assert (successor.blocked_by_task_id, successor.title) == (build["child"], "Use the digest")
    assert (await fresh(db_session, Task, other["id"])).blocked_by_task_id == unrelated["id"]
    source = await fresh(db_session, Task, build["source"])
    assert (source.status, source.package_build) == ("in_dev", build_body(owner)["package_build"])
    await assert_outcome_settled_once(db_session, build)
    assert before == {queue: await redis_client.xlen(queue) for queue in QUEUES}

    # A lost response repeats the same patch: same child, no second write anywhere.
    events = await event_count(db_session, build)
    await complete(async_client, build)
    installs = [t for t in await story_tasks(db_session, owner["story"]) if t.type == "install"]
    assert [t.id for t in installs] == [build["child"]]
    assert await event_count(db_session, build) == events
    await assert_outcome_settled_once(db_session, build)

    done = await produce_done(async_client, build)
    assert done.status_code == 200 and done.json()["status"] == "done", done.text
    refused = await post(
        async_client, "/api/work-admission/engineering-dispatches", {"task_id": build["successor"]}
    )
    assert (refused["outcome"], refused["reason"]) == ("refused", "blocker_unresolved")
    admitted = await command(async_client, {"id": build["child"]}, "admit")
    assert (admitted["outcome"], admitted["install"]) == ("admitted", local_install(owner))
    await assert_outcome_settled_once(db_session, build)


@pytest.mark.asyncio
async def test_concurrent_identical_deliveries_create_one_install(async_client, db_session):
    build = await admitted_build(async_client, db_session)
    answers = await asyncio.gather(
        *[async_client.patch(f"/api/runs/{build['run']}", json=success()) for _ in range(2)]
    )
    # The loser either sees the terminal Run or refuses on the roster the INSTALL changed.
    for answer in answers:
        assert answer.status_code == 200 or (
            answer.status_code == 409 and "story_roster_changed" in answer.text
        ), answer.text
    assert 200 in [answer.status_code for answer in answers]
    events = await event_count(db_session, build)
    await complete(async_client, build)
    installs = [
        t for t in await story_tasks(db_session, build["owner"]["story"]) if t.type == "install"
    ]
    assert [t.id for t in installs] == [build["child"]]
    assert (await fresh(db_session, Task, build["successor"])).blocked_by_task_id == build["child"]
    assert await event_count(db_session, build) == events
    await assert_outcome_settled_once(db_session, build)


@pytest.mark.asyncio
async def test_a_rolled_back_delivery_leaves_no_half_handoff_and_its_retry_completes(
    async_client, db_session, monkeypatch
):
    from src.routers import runs

    build = await admitted_build(async_client, db_session)

    async def lost(*args):
        raise RuntimeError("settlement interrupted")

    monkeypatch.setattr(runs, "finalize_engineering_reservation", lost)
    with pytest.raises(RuntimeError):
        await async_client.patch(f"/api/runs/{build['run']}", json=success())
    monkeypatch.undo()
    assert (await fresh(db_session, Run, build["run"])).status == "running"
    await assert_hold_active(db_session, build["run"])
    await assert_no_handoff(db_session, build)

    await complete(async_client, build)
    assert (await fresh(db_session, Task, build["child"])).install["commit"] == COMMIT
    assert (await fresh(db_session, Task, build["successor"])).blocked_by_task_id == build["child"]
    await assert_outcome_settled_once(db_session, build)


async def assert_stopped_for_review(db_session, build, cause):
    source = await fresh(db_session, Task, build["source"])
    assert source.status == "waiting_human_review"
    events = (
        await db_session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == source.id).order_by(TaskEvent.id)
        )
    ).all()
    assert events[-1].details == {"local_package_handoff_refused": cause, "run_id": build["run"]}
    story = await fresh(db_session, Story, build["owner"]["story"])
    assert story.status == "waiting_human_review"
    assert story.quarantine_reason["code"] == "local_package_handoff_refused"
    assert cause in story.quarantine_reason["detail"]
    assert story.owner_notification["state"] == "owed"
    assert "engineering work completed" in story.owner_notification["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "cause"),
    [
        ("missing", "commit_missing"),
        ("partial", "commit_invalid"),
        ("foreign_repository", "repository_unowned"),
        ("collision", "install_identity_conflict"),
    ],
)
async def test_a_current_output_that_cannot_form_its_install_stops_for_a_person(
    async_client, db_session, output, cause
):
    telegram = await buyer(async_client)
    owner = await product(async_client, telegram)
    build = await admitted_build(async_client, db_session, owner)
    body = success({"missing": None, "partial": COMMIT[:12]}.get(output, COMMIT))
    if output == "foreign_repository":
        other = await product(async_client, telegram)
        source = await fresh(db_session, Task, build["source"])
        source.repository_id = other["repository"]
        await db_session.commit()
    if output == "collision":
        db_session.add(
            Task(
                id=build["child"],
                project_id=uuid.UUID(owner["project"]),
                type="feature",
                title="Occupies the install identity",
                status="todo",
                story_id=owner["story"],
                repository_id=owner["repository"],
            )
        )
        await db_session.commit()

    await complete(async_client, build, body)

    await assert_stopped_for_review(db_session, build, cause)
    await assert_outcome_settled_once(db_session, build, body)
    successor = await fresh(db_session, Task, build["successor"])
    assert successor.blocked_by_task_id == build["source"]
    child = await fresh(db_session, Task, build["child"])
    if output == "collision":
        assert (child.type, child.install, child.status) == ("feature", None, "todo")
    else:
        assert child is None
    events = await event_count(db_session, build)
    await complete(async_client, build, body)
    assert await event_count(db_session, build) == events
    await assert_stopped_for_review(db_session, build, cause)


@pytest.mark.asyncio
async def test_a_second_completed_generation_never_replaces_the_pinned_install(
    async_client, db_session
):
    build = await admitted_build(async_client, db_session)
    await complete(async_client, build)
    owner = build["owner"]
    second = Run(
        id=f"eng-second-{uuid.uuid4().hex[:8]}",
        type="engineering",
        status="running",
        project_id=uuid.UUID(owner["project"]),
        story_id=owner["story"],
        task_id=build["source"],
        run_metadata={"iteration": 0},
    )
    db_session.add(second)
    await db_session.commit()
    changed = success("3" * 40)
    response = await async_client.patch(f"/api/runs/{second.id}", json=changed)
    assert response.status_code == 200, response.text

    child = await fresh(db_session, Task, build["child"])
    assert (child.install, child.status) == (local_install(owner), "todo")
    assert (await fresh(db_session, Run, second.id)).result == changed["result"]
    await assert_stopped_for_review(
        db_session, {**build, "run": second.id}, "install_identity_conflict"
    )
    installs = [t for t in await story_tasks(db_session, owner["story"]) if t.type == "install"]
    assert [t.id for t in installs] == [build["child"]]
    assert (await fresh(db_session, Task, build["successor"])).blocked_by_task_id == build["child"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fence",
    [
        "stale_iteration",
        "missing_iteration",
        "reopened_cycle",
        "foreign_run_project",
        "live_competitor",
        "unadmitted",
        "cancelled",
        "stopped",
    ],
)
async def test_a_receipt_without_work_authority_keeps_only_its_paid_outcome(
    async_client, db_session, fence
):
    telegram = await buyer(async_client)
    owner = await product(async_client, telegram)
    build = await admitted_build(async_client, db_session, owner)
    source = await fresh(db_session, Task, build["source"])
    run = await fresh(db_session, Run, build["run"])
    if fence == "stale_iteration":
        source.current_iteration = 1
    elif fence == "missing_iteration":
        run.run_metadata = {k: v for k, v in run.run_metadata.items() if k != "iteration"}
    elif fence == "reopened_cycle":
        story = await fresh(db_session, Story, owner["story"])
        story.reopened_at = datetime.now(UTC)
    elif fence == "foreign_run_project":
        run.project_id = uuid.UUID((await product(async_client, telegram))["project"])
    elif fence == "live_competitor":
        db_session.add(
            Run(
                id=f"eng-rival-{uuid.uuid4().hex[:8]}",
                type="engineering",
                status="running",
                project_id=run.project_id,
                story_id=owner["story"],
                task_id=build["successor"],
                run_metadata={"iteration": 0},
            )
        )
    elif fence == "unadmitted":
        source.dispatch_admitted = False
    await db_session.commit()
    if fence == "cancelled":
        await post(async_client, f"/api/tasks/{build['source']}/transition?to_status=cancelled", {})
    if fence == "stopped":
        worker = {"run_metadata": {"worker_id": f"worker-{uuid.uuid4().hex[:8]}"}}
        assert (await async_client.patch(f"/api/runs/{build['run']}", json=worker)).is_success
        await post(async_client, f"/api/stories/{owner['story']}/human-review", {})
    story_before = await fresh(db_session, Story, owner["story"])
    stop_before = (
        story_before.status,
        story_before.quarantine_reason,
        story_before.engineering_stop,
    )
    status_before = (await fresh(db_session, Task, build["source"])).status

    await complete(async_client, build)

    await assert_no_handoff(db_session, build)
    await assert_outcome_settled_once(db_session, build)
    story = await fresh(db_session, Story, owner["story"])
    assert (story.status, story.quarantine_reason, story.engineering_stop) == stop_before
    assert (await fresh(db_session, Task, build["source"])).status == status_before
    await produce_done(async_client, build)
    assert (await fresh(db_session, Task, build["source"])).status != "done"


@pytest.mark.asyncio
async def test_a_feature_without_a_build_intent_completes_as_before(async_client, db_session):
    build = await admitted_build(async_client, db_session, package_build=None)
    await complete(async_client, build)
    done = await produce_done(async_client, build)
    assert done.status_code == 200 and done.json()["status"] == "done", done.text
    tasks = await story_tasks(db_session, build["owner"]["story"])
    assert sorted(t.type for t in tasks) == ["feature", "feature"]
    assert (await fresh(db_session, Task, build["successor"])).blocked_by_task_id == build["source"]
    await assert_outcome_settled_once(db_session, build)


@pytest.mark.asyncio
async def test_generic_done_routes_refuse_a_build_without_its_pinned_install(async_client):
    owner = await product(async_client, await buyer(async_client))
    source = await post(async_client, "/api/tasks/", build_body(owner), 201)
    await post(async_client, f"/api/tasks/{source['id']}/start")
    # A hand-made INSTALL of the same target and a note naming the commit prove nothing.
    await post(
        async_client,
        "/api/tasks/",
        {
            "project_id": owner["project"],
            "repository_id": owner["repository"],
            "story_id": owner["story"],
            "title": "Install notes digest",
            "type": "install",
            "status": "todo",
            "install": local_install(owner),
            "blocked_by_task_id": source["id"],
        },
        201,
    )
    await post(
        async_client,
        f"/api/tasks/{source['id']}/events",
        {"event_type": "iteration_end", "details": {"commit_sha": COMMIT}, "actor": "admin"},
        201,
    )
    completed = await async_client.post(f"/api/tasks/{source['id']}/complete", json={})
    assert completed.status_code == 409 and "local_package_handoff_required" in completed.text
    for step in ("in_ci", "testing"):
        await post(async_client, f"/api/tasks/{source['id']}/transition?to_status={step}", {})
    done = await async_client.post(f"/api/tasks/{source['id']}/transition?to_status=done", json={})
    assert done.status_code == 409 and "local_package_handoff_required" in done.text
    assert (await async_client.get(f"/api/tasks/{source['id']}")).json()["status"] == "testing"


@pytest.mark.asyncio
async def test_commit_recovery_of_a_build_fails_closed_and_keeps_its_stop(async_client, db_session):
    build = await admitted_build(async_client, db_session)
    failed = {"status": "failed", "result": {"engineering_status": "failed"}}
    assert (await async_client.patch(f"/api/runs/{build['run']}", json=failed)).is_success
    stopped = await post(async_client, f"/api/stories/{build['owner']['story']}/human-review", {})
    before = await fresh(db_session, Story, build["owner"]["story"])
    held = (before.status, before.quarantine_reason, before.engineering_stop)
    response = await async_client.post(
        f"/api/stories/{build['owner']['story']}/recover-commit",
        json={
            "attempt_id": build["run"],
            "commit_sha": COMMIT,
            "stop_id": stopped["engineering_stop"]["id"],
            "adopt_preserved_commit": True,
        },
    )
    assert response.status_code == 409 and "local_package_handoff_unsupported" in response.text
    assert await db_session.get(CommitRecovery, build["run"]) is None
    story = await fresh(db_session, Story, build["owner"]["story"])
    assert (story.status, story.quarantine_reason, story.engineering_stop) == held
    run = await fresh(db_session, Run, build["run"])
    assert (run.status, run.result) == ("failed", failed["result"])
    await assert_no_handoff(db_session, build)
