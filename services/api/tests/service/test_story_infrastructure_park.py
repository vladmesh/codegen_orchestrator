"""Service proofs for the atomic park of a pre-agent infrastructure refusal.

Every test drives `POST /api/stories/{id}/park-infrastructure-refusal` through
the real database transaction and reads the committed rows back, so a partial
park is observable here if the transaction ever exposes one.
"""

import asyncio
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_dispatch import EngineeringDispatchRefusal
from shared.contracts.dto.story import StoryStatus
from shared.models import Run, Story
from src.routers import _story_actions
from src.routers._story_helpers import _land_on

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
KEY = "engineering_infrastructure"
DETAIL = "Engineering worker creation was refused: executor unavailable."
PARK_ACTION = "park_infrastructure_refusal"


async def _story_task(client: AsyncClient, *, task_status: str = "todo") -> tuple[str, str]:
    created_story = await client.post(
        "/api/stories/", json={"project_id": PROJECT_ID, "title": "Infrastructure park"}
    )
    assert created_story.status_code == 201, created_story.text
    story_id = created_story.json()["id"]
    created_task = await client.post(
        "/api/tasks/",
        json={
            "project_id": PROJECT_ID,
            "story_id": story_id,
            "title": "Refused task",
            "status": "todo",
        },
    )
    assert created_task.status_code == 201, created_task.text
    task_id = created_task.json()["id"]
    started = await client.post(f"/api/stories/{story_id}/start", json={"actor": "test"})
    assert started.status_code == 200, started.text
    for hop in {"todo": [], "in_dev": ["in_dev"], "failed": ["in_dev", "failed"]}[task_status]:
        moved = await client.post(
            f"/api/tasks/{task_id}/transition?to_status={hop}", json={"actor": "test"}
        )
        assert moved.status_code == 200, moved.text
    return story_id, task_id


def _park(task_id: str, **changes) -> dict:
    return {
        "execution_phase": "pre_agent_refused",
        "refusal": "executor_unavailable",
        "task_id": task_id,
        "attempt_id": "eng-park-1",
        "detail": DETAIL,
        **changes,
    }


async def _park_call(client: AsyncClient, story_id: str, park: dict):
    return await client.post(
        f"/api/stories/{story_id}/park-infrastructure-refusal",
        json={"park": park, "actor": "dispatcher"},
    )


async def _state(client: AsyncClient, story_id: str, task_id: str) -> tuple:
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    story = (await client.get(f"/api/stories/{story_id}")).json()
    return (
        task["status"],
        task["failure_metadata"],
        task["current_iteration"],
        story["status"],
        story["quarantine_reason"],
    )


async def _park_hops(client: AsyncClient, task_id: str) -> list[str]:
    events = (await client.get(f"/api/tasks/{task_id}/events")).json()
    return sorted(
        event["to_status"]
        for event in events
        if (event["details"] or {}).get("action") == PARK_ACTION
    )


@pytest.mark.parametrize(
    "task_status,hops",
    [
        ("todo", ["in_dev", "waiting_human_review"]),
        ("in_dev", ["waiting_human_review"]),
        ("failed", ["waiting_human_review"]),
    ],
)
@pytest.mark.asyncio
async def test_park_commits_both_rows_evidence_and_owner_notice_together(
    async_client: AsyncClient, _tasks_project, task_status: str, hops: list[str]
) -> None:
    story_id, task_id = await _story_task(async_client, task_status=task_status)
    park = _park(task_id)

    response = await _park_call(async_client, story_id, park)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["disposition"], body["task_status"], body["story_status"]) == (
        "parked",
        "waiting_human_review",
        "waiting_human_review",
    )
    assert await _state(async_client, story_id, task_id) == (
        "waiting_human_review",
        {KEY: park},
        0,
        "waiting_human_review",
        {KEY: park},
    )
    assert await _park_hops(async_client, task_id) == hops
    notice = (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json()
    assert (notice["event"], notice["state"], notice["text"], notice["terminal_status"]) == (
        "story_blocked",
        "owed",
        DETAIL,
        "waiting_human_review",
    )

    # The committed park is exactly what the one operator recovery action accepts.
    retried = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt",
        json={
            "task_id": task_id,
            "attempt_id": "eng-park-1",
            "refusal": "executor_unavailable",
            "actor": "admin",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["outcome"] == "retried"
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)


@pytest.mark.asyncio
async def test_repeating_the_same_park_is_a_typed_noop(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    park = _park(task_id)

    first = await _park_call(async_client, story_id, park)
    notice = (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json()
    repeated = await _park_call(async_client, story_id, park)

    assert [first.json()["disposition"], repeated.json()["disposition"]] == [
        "parked",
        "already_parked",
    ]
    assert await _park_hops(async_client, task_id) == ["in_dev", "waiting_human_review"]
    assert (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json() == notice
    assert repeated.json()["current_iteration"] == 0


@pytest.mark.asyncio
async def test_concurrent_equal_parks_converge_to_one_park(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    park = _park(task_id)

    responses = await asyncio.gather(
        _park_call(async_client, story_id, park),
        _park_call(async_client, story_id, park),
    )

    assert sorted(response.json()["disposition"] for response in responses) == [
        "already_parked",
        "parked",
    ]
    assert await _park_hops(async_client, task_id) == ["in_dev", "waiting_human_review"]
    assert (await _state(async_client, story_id, task_id))[2] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_id": "eng-park-2"},
        {"refusal": "project_locked"},
        {"detail": "A different refusal detail."},
    ],
)
@pytest.mark.asyncio
async def test_a_different_park_fails_closed_without_partial_change(
    async_client: AsyncClient, _tasks_project, change: dict
) -> None:
    story_id, task_id = await _story_task(async_client)
    parked = await _park_call(async_client, story_id, _park(task_id))
    assert parked.json()["disposition"] == "parked"
    before = await _state(async_client, story_id, task_id)

    response = await _park_call(async_client, story_id, _park(task_id, **change))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_infrastructure_reason"
    assert await _state(async_client, story_id, task_id) == before


@pytest.mark.asyncio
async def test_a_task_of_another_story_fails_closed(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, _ = await _story_task(async_client)
    other_story_id, other_task_id = await _story_task(async_client)

    response = await _park_call(async_client, story_id, _park(other_task_id))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "wrong_task"
    assert await _state(async_client, other_story_id, other_task_id) == (
        "todo",
        None,
        0,
        "in_progress",
        None,
    )


@pytest.mark.asyncio
async def test_a_story_already_in_human_review_for_another_reason_fails_closed(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="in_dev")
    reviewed = await async_client.post(f"/api/stories/{story_id}/human-review", json={"actor": "t"})
    assert reviewed.status_code == 200, reviewed.text

    response = await _park_call(async_client, story_id, _park(task_id))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_in_human_review"
    assert await _state(async_client, story_id, task_id) == (
        "in_dev",
        None,
        0,
        "waiting_human_review",
        None,
    )


@pytest.mark.parametrize("action,story_status", [("fail", "failed"), ("archive", "archived")])
@pytest.mark.asyncio
async def test_a_terminal_story_is_contained_without_reopening_or_changing_the_task(
    async_client: AsyncClient, _tasks_project, action: str, story_status: str
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    ended = await async_client.post(f"/api/stories/{story_id}/{action}", json={"actor": "test"})
    assert ended.status_code == 200, ended.text

    response = await _park_call(async_client, story_id, _park(task_id))

    assert response.status_code == 200, response.text
    assert response.json()["disposition"] == "ineligible_story"
    assert await _state(async_client, story_id, task_id) == (
        "failed",
        None,
        0,
        story_status,
        None,
    )
    assert await _park_hops(async_client, task_id) == []
    notice = await async_client.get(f"/api/stories/{story_id}/owner-notification")
    assert notice.status_code == 404


@pytest.mark.asyncio
async def test_a_story_that_turns_terminal_while_the_park_waits_is_contained(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    """The park serializes on the story row and decides on the status that committed."""
    story_id, task_id = await _story_task(async_client, task_status="failed")
    story = await db_session.scalar(select(Story).where(Story.id == story_id).with_for_update())

    pending = asyncio.create_task(_park_call(async_client, story_id, _park(task_id)))
    await asyncio.sleep(0.5)
    assert not pending.done()
    _land_on(story, StoryStatus.FAILED)
    await db_session.commit()
    response = await pending

    assert response.status_code == 200, response.text
    assert response.json()["disposition"] == "ineligible_story"
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, "failed", None)


@pytest.mark.asyncio
async def test_a_failure_inside_the_transaction_leaves_no_partial_park(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)

    def _fail_after_task_hops(*_args, **_kwargs) -> None:
        raise RuntimeError("injected failure after the task hops")

    monkeypatch.setattr(_story_actions, "_do_transition", _fail_after_task_hops)
    with pytest.raises(RuntimeError, match="injected failure"):
        await _park_call(async_client, story_id, _park(task_id))
    monkeypatch.undo()

    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)
    assert await _park_hops(async_client, task_id) == []
    notice = await async_client.get(f"/api/stories/{story_id}/owner-notification")
    assert notice.status_code == 404

    parked = await _park_call(async_client, story_id, _park(task_id))
    assert parked.json()["disposition"] == "parked"


@pytest.mark.asyncio
async def test_a_refused_run_must_match_the_park(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    started_id = f"eng-started-{uuid.uuid4().hex[:8]}"
    refused_id = f"eng-refused-{uuid.uuid4().hex[:8]}"
    for run_id, execution in (
        (started_id, {"execution_phase": "agent_started"}),
        (
            refused_id,
            {"execution_phase": "pre_agent_refused", "infrastructure_refusal": "project_locked"},
        ),
    ):
        db_session.add(
            Run(
                id=run_id,
                type="engineering",
                status="failed",
                project_id=uuid.UUID(PROJECT_ID),
                task_id=task_id,
                story_id=story_id,
                run_metadata={},
                result={"engineering_status": "failed", "execution": execution},
            )
        )
    await db_session.commit()

    stale = await _park_call(
        async_client,
        story_id,
        _park(task_id, attempt_id=started_id, refusal="project_locked"),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_attempt_fence"
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, "in_progress", None)

    parked = await _park_call(
        async_client,
        story_id,
        _park(task_id, attempt_id=refused_id, refusal="project_locked"),
    )
    assert parked.json()["disposition"] == "parked"
    runs = await db_session.scalars(select(Run.status).where(Run.task_id == task_id))
    assert sorted(runs.all()) == ["failed", "failed"]


@pytest.mark.asyncio
async def test_a_non_admin_user_cannot_park(async_client: AsyncClient, _tasks_project) -> None:
    story_id, task_id = await _story_task(async_client)
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"park-{telegram_id}"}
    )
    assert created.status_code == 201, created.text

    response = await async_client.post(
        f"/api/stories/{story_id}/park-infrastructure-refusal",
        json={"park": _park(task_id), "actor": "user"},
        headers={"X-Telegram-ID": str(telegram_id)},
    )

    assert response.status_code == 403
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)


@pytest.mark.asyncio
async def test_admission_fences_a_parked_story_and_a_parked_task_without_an_attempt(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    sibling = await async_client.post(
        "/api/tasks/",
        json={"project_id": PROJECT_ID, "story_id": story_id, "title": "Sibling", "status": "todo"},
    )
    assert sibling.status_code == 201, sibling.text
    parked = await _park_call(async_client, story_id, _park(task_id))
    assert parked.json()["disposition"] == "parked"
    evidence_task = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": PROJECT_ID,
            "title": "Carries a park",
            "status": "todo",
            "failure_metadata": {KEY: _park("elsewhere")},
        },
    )
    assert evidence_task.status_code == 201, evidence_task.text

    for fenced_id in (sibling.json()["id"], evidence_task.json()["id"]):
        decision = await async_client.post(
            "/api/work-admission/engineering-dispatches", json={"task_id": fenced_id}
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["reason"] == EngineeringDispatchRefusal.INFRASTRUCTURE_PARKED
        assert decision.json()["run_id"] is None
        runs = await db_session.scalars(select(Run.id).where(Run.task_id == fenced_id))
        assert runs.all() == []
