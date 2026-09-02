"""Service proofs for the Product Brief coverage-to-dispatch boundary.

Everything here runs against the real database, because everything here is a
property of rows and locks: the admission step is idempotent because it decides
under `SELECT ... FOR UPDATE` on the brief, the claim yields one owner because
two claims queue on that same row, and the dispatch gate refuses because the
admission point reads the Task column the admission step writes.

Where the property is a race, the test drives a genuinely concurrent second
transaction — a lock parked on the row by a third session, both callers fired
with `asyncio.gather`, the lock released — rather than mocking a helper and
asserting it was called.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    ProductBriefAdmissionOutcome,
    ProductBriefPlanningAttemptOutcome,
)
from shared.models import ProductBrief, Task

BRIEFS_URL = "/api/product-briefs"
DISPATCH_URL = "/api/work-admission/engineering-dispatches"

#: How long the two racing claims are given to reach the brief row while a third
#: session holds it. Without the lock they both run to completion here, which is
#: exactly the failure the race is meant to show.
_RACE_WINDOW_SECONDS = 1.0

_CONTENT = {
    "summary": "A bot that tracks reading",
    "must_requirements": [
        {"id": "r1", "text": "It stores a book"},
        {"id": "r2", "text": "It lists the books"},
    ],
}


# --- fixtures written through the real endpoints ------------------------------


async def _owner(client: AsyncClient) -> int:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"brief-{telegram_id}"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return telegram_id


async def _project(client: AsyncClient, telegram_id: int) -> str:
    project_id = str(uuid.uuid4())
    created = await client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Product Brief admission",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return project_id


async def _story(client: AsyncClient, project_id: str) -> str:
    created = await client.post(
        "/api/stories/", json={"project_id": project_id, "title": "One branch"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


async def _confirmed_brief(client: AsyncClient, project_id: str, story_id: str | None) -> str:
    """A confirmed brief, bound to its story unless the test wants it unbound."""
    created = await client.post(
        f"{BRIEFS_URL}/",
        json={
            "project_id": project_id,
            "title": "Reading tracker",
            "content": _CONTENT,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    brief_id = created.json()["id"]
    confirmed = await client.post(
        f"{BRIEFS_URL}/{brief_id}/confirm",
        json={"request_id": f"conf-{uuid.uuid4().hex}", "content": _CONTENT},
    )
    assert confirmed.status_code == HTTPStatus.OK, confirmed.text
    if story_id is not None:
        bound = await client.post(f"{BRIEFS_URL}/{brief_id}/story", json={"story_id": story_id})
        assert bound.status_code == HTTPStatus.OK, bound.text
    return brief_id


async def _planned_brief(client: AsyncClient, project_id: str) -> tuple[str, str, str]:
    """A confirmed, bound brief with one live architect. -> (brief, story, attempt)"""
    story_id = await _story(client, project_id)
    brief_id = await _confirmed_brief(client, project_id, story_id)
    claim = await client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    assert claim.status_code == HTTPStatus.OK, claim.text
    assert claim.json()["outcome"] == ProductBriefPlanningAttemptOutcome.CLAIMED
    return brief_id, story_id, claim.json()["planning_attempt_id"]


async def _planned_task(
    client: AsyncClient, project_id: str, story_id: str, attempt: str, title: str = "Planned"
) -> str:
    created = await client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": title,
            "status": "todo",
            "story_id": story_id,
            "planning_attempt_id": attempt,
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    body = created.json()
    # A brief-backed task is planned, not yet dispatchable: this is the whole
    # point of the boundary, so it is asserted at the moment of creation.
    assert body["dispatch_admitted"] is False
    assert body["planning_attempt_id"] == attempt
    return body["id"]


async def _cover(
    client: AsyncClient, brief_id: str, requirement_id: str, attempt: str, **disposition
):
    return await client.put(
        f"{BRIEFS_URL}/{brief_id}/coverage/{requirement_id}",
        json={
            "requirement_id": requirement_id,
            "planning_attempt_id": attempt,
            **disposition,
        },
    )


async def _admit(client: AsyncClient, brief_id: str, attempt: str):
    return await client.post(
        f"{BRIEFS_URL}/{brief_id}/admit", json={"planning_attempt_id": attempt}
    )


async def _decide(client: AsyncClient, task_id: str) -> dict:
    response = await client.post(DISPATCH_URL, json={"task_id": task_id})
    assert response.status_code == HTTPStatus.OK, response.text
    return response.json()


async def _go_stale(db_session: AsyncSession, brief_id: str) -> None:
    """Age the owner's heartbeat past the timeout. Setup, not a stubbed clock."""
    brief = await db_session.get(ProductBrief, brief_id)
    brief.planning_attempt_heartbeat_at = datetime.now(UTC) - timedelta(
        seconds=PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS * 2
    )
    await db_session.commit()
    db_session.expunge(brief)


# --- the one durable, idempotent admission step -------------------------------


@pytest.mark.asyncio
async def test_admitting_twice_releases_the_plan_exactly_once(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A retry of a succeeded admission is a replay, not a second release."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    first_task = await _planned_task(async_client, project_id, story_id, attempt, "First")
    second_task = await _planned_task(async_client, project_id, story_id, attempt, "Second")
    assert (
        await _cover(async_client, brief_id, "r1", attempt, task_id=first_task)
    ).status_code == (HTTPStatus.OK)
    assert (
        await _cover(async_client, brief_id, "r2", attempt, task_id=second_task)
    ).status_code == HTTPStatus.OK

    first = await _admit(async_client, brief_id, attempt)
    assert first.status_code == HTTPStatus.OK, first.text
    assert first.json()["outcome"] == ProductBriefAdmissionOutcome.ADMITTED
    assert sorted(first.json()["released_task_ids"]) == sorted([first_task, second_task])

    second = await _admit(async_client, brief_id, attempt)
    assert second.status_code == HTTPStatus.OK, second.text
    assert second.json()["outcome"] == ProductBriefAdmissionOutcome.ALREADY_ADMITTED
    # Nothing released twice, and the boundary keeps the instant it was crossed.
    assert second.json()["released_task_ids"] == []
    assert second.json()["coverage_admitted_at"] == first.json()["coverage_admitted_at"]

    for task_id in (first_task, second_task):
        task = await db_session.get(Task, task_id)
        await db_session.refresh(task)
        assert task.dispatch_admitted is True


@pytest.mark.asyncio
async def test_an_incomplete_brief_names_its_missing_requirements_and_releases_nothing(
    async_client: AsyncClient, db_session: AsyncSession
):
    """An undisposed requirement is a gap, and the answer says which one."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    task_id = await _planned_task(async_client, project_id, story_id, attempt)
    assert (await _cover(async_client, brief_id, "r1", attempt, task_id=task_id)).status_code == (
        HTTPStatus.OK
    )

    answer = await _admit(async_client, brief_id, attempt)

    assert answer.status_code == HTTPStatus.OK, answer.text
    assert answer.json()["outcome"] == ProductBriefAdmissionOutcome.INCOMPLETE
    assert answer.json()["missing_requirement_ids"] == ["r2"]
    assert answer.json()["released_task_ids"] == []
    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert task.dispatch_admitted is False
    brief = await db_session.get(ProductBrief, brief_id)
    await db_session.refresh(brief)
    assert brief.coverage_admitted_at is None
    # The planner still owns its incomplete plan: an incomplete answer is not a
    # release of the fence either.
    assert brief.planning_attempt_active is True


@pytest.mark.asyncio
async def test_a_returned_requirement_is_a_disposition(async_client: AsyncClient):
    """ "We are not doing this, and here is why" completes the coverage."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    task_id = await _planned_task(async_client, project_id, story_id, attempt)
    await _cover(async_client, brief_id, "r1", attempt, task_id=task_id)
    await _cover(async_client, brief_id, "r2", attempt, returned_reason="needs a paid API")

    answer = await _admit(async_client, brief_id, attempt)

    assert answer.json()["outcome"] == ProductBriefAdmissionOutcome.ADMITTED
    assert answer.json()["released_task_ids"] == [task_id]


# --- exactly one live architect per incomplete plan ---------------------------


@pytest.mark.asyncio
async def test_two_concurrent_claims_yield_exactly_one_live_attempt(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Both architects arrive at once; the brief row decides which one owns it.

    A third session parks a lock on the brief so both requests are genuinely in
    flight when it is released — no mocked helper, no serialized calls dressed
    up as a race.
    """
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    brief_id = await _confirmed_brief(async_client, project_id, story_id)

    parked = (
        await db_session.execute(
            select(ProductBrief).where(ProductBrief.id == brief_id).with_for_update()
        )
    ).scalar_one_or_none()
    assert parked is not None, "the brief is not visible to the locking session"
    claims = [
        asyncio.create_task(async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim"))
        for _ in range(2)
    ]
    try:
        await asyncio.sleep(_RACE_WINDOW_SECONDS)
    finally:
        await db_session.rollback()
    responses = await asyncio.gather(*claims)

    bodies = [response.json() for response in responses]
    assert all(response.status_code == HTTPStatus.OK for response in responses), bodies
    assert sorted(body["outcome"] for body in bodies) == [
        ProductBriefPlanningAttemptOutcome.CLAIMED,
        ProductBriefPlanningAttemptOutcome.IN_PROGRESS,
    ]
    # The loser is told which attempt owns the plan — the winner's, not one of
    # its own, so a second architect never walks away holding a rival id.
    attempts = {body["planning_attempt_id"] for body in bodies}
    assert len(attempts) == 1
    brief = await db_session.get(ProductBrief, brief_id)
    await db_session.refresh(brief)
    assert brief.planning_attempt_id == attempts.pop()
    assert brief.planning_attempt_active is True


@pytest.mark.asyncio
async def test_a_fresh_claim_is_not_reissued_and_a_stale_one_is_taken_over(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The heartbeat is the whole difference between a retry and a takeover."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, _story_id, first_attempt = await _planned_brief(async_client, project_id)

    retry = await async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    assert retry.json()["outcome"] == ProductBriefPlanningAttemptOutcome.IN_PROGRESS
    assert retry.json()["planning_attempt_id"] == first_attempt

    beat = await async_client.post(
        f"{BRIEFS_URL}/{brief_id}/planning-attempts/heartbeat",
        json={"planning_attempt_id": first_attempt},
    )
    assert beat.status_code == HTTPStatus.OK, beat.text

    await _go_stale(db_session, brief_id)
    takeover = await async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    assert takeover.json()["outcome"] == ProductBriefPlanningAttemptOutcome.CLAIMED
    assert takeover.json()["planning_attempt_id"] != first_attempt

    # The superseded architect is out: it cannot beat its own attempt back to life.
    stale_beat = await async_client.post(
        f"{BRIEFS_URL}/{brief_id}/planning-attempts/heartbeat",
        json={"planning_attempt_id": first_attempt},
    )
    assert stale_beat.status_code == HTTPStatus.CONFLICT, stale_beat.text


@pytest.mark.asyncio
async def test_a_superseded_planner_can_neither_record_coverage_nor_admit(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A planning retry cannot release a duplicate plan, from either direction."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, first_attempt = await _planned_brief(async_client, project_id)
    abandoned = await _planned_task(async_client, project_id, story_id, first_attempt, "Abandoned")

    await _go_stale(db_session, brief_id)
    takeover = await async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    second_attempt = takeover.json()["planning_attempt_id"]

    refused_coverage = await _cover(async_client, brief_id, "r1", first_attempt, task_id=abandoned)
    assert refused_coverage.status_code == HTTPStatus.CONFLICT, refused_coverage.text
    refused_admit = await _admit(async_client, brief_id, first_attempt)
    assert refused_admit.status_code == HTTPStatus.CONFLICT, refused_admit.text
    # It cannot plan any more work into the story either.
    refused_task = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "Too late",
            "status": "todo",
            "story_id": story_id,
            "planning_attempt_id": first_attempt,
        },
    )
    assert refused_task.status_code == HTTPStatus.CONFLICT, refused_task.text

    task = await db_session.get(Task, abandoned)
    await db_session.refresh(task)
    assert task.dispatch_admitted is False
    assert second_attempt != first_attempt


@pytest.mark.asyncio
async def test_an_admission_releases_only_the_tasks_of_its_own_attempt(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The replacement's admission leaves the abandoned plan where it is."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, first_attempt = await _planned_brief(async_client, project_id)
    abandoned = await _planned_task(async_client, project_id, story_id, first_attempt, "Abandoned")
    assert (
        await _cover(async_client, brief_id, "r1", first_attempt, task_id=abandoned)
    ).status_code == HTTPStatus.OK

    await _go_stale(db_session, brief_id)
    takeover = await async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    second_attempt = takeover.json()["planning_attempt_id"]
    replacement = await _planned_task(
        async_client, project_id, story_id, second_attempt, "Replacement"
    )

    # The stale attempt's disposition of r1 does not count for the new one: a
    # plan taken over starts from nothing covered, because the row it inherited
    # points at a task this admission will never release.
    still_incomplete = await _admit(async_client, brief_id, second_attempt)
    assert still_incomplete.json()["outcome"] == ProductBriefAdmissionOutcome.INCOMPLETE
    assert still_incomplete.json()["missing_requirement_ids"] == ["r1", "r2"]

    await _cover(async_client, brief_id, "r1", second_attempt, task_id=replacement)
    await _cover(async_client, brief_id, "r2", second_attempt, returned_reason="dropped")
    admitted = await _admit(async_client, brief_id, second_attempt)

    assert admitted.json()["outcome"] == ProductBriefAdmissionOutcome.ADMITTED
    assert admitted.json()["released_task_ids"] == [replacement]
    orphan = await db_session.get(Task, abandoned)
    await db_session.refresh(orphan)
    assert orphan.dispatch_admitted is False


@pytest.mark.asyncio
async def test_a_covering_task_must_belong_to_this_attempt(async_client: AsyncClient):
    """Coverage that pointed at somebody else's task would admit a plan twice."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, _story_id, attempt = await _planned_brief(async_client, project_id)
    other_brief, other_story, other_attempt = await _planned_brief(async_client, project_id)
    foreign = await _planned_task(async_client, project_id, other_story, other_attempt)

    refused = await _cover(async_client, brief_id, "r1", attempt, task_id=foreign)

    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, refused.text
    assert other_brief != brief_id


# --- the gate, one condition in the declared admission point -------------------


@pytest.mark.asyncio
async def test_the_dispatch_point_refuses_a_task_of_an_unadmitted_brief(
    async_client: AsyncClient,
):
    """A `todo` status is not dispatch authority while the plan is unreleased.

    One typed refusal from the one admission point — and after the brief's
    admission, the very same task is admitted with nothing else changed.
    """
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    task_id = await _planned_task(async_client, project_id, story_id, attempt)

    refused = await _decide(async_client, task_id)
    assert refused["outcome"] == EngineeringDispatchOutcome.REFUSED
    assert refused["reason"] == EngineeringDispatchRefusal.PRODUCT_BRIEF_NOT_ADMITTED
    assert refused["run_id"] is None

    await _cover(async_client, brief_id, "r1", attempt, task_id=task_id)
    await _cover(async_client, brief_id, "r2", attempt, returned_reason="dropped")
    assert (await _admit(async_client, brief_id, attempt)).json()["outcome"] == (
        ProductBriefAdmissionOutcome.ADMITTED
    )

    admitted = await _decide(async_client, task_id)
    assert admitted["outcome"] == EngineeringDispatchOutcome.ADMITTED


@pytest.mark.asyncio
async def test_the_brief_refusal_cannot_be_overridden(async_client: AsyncClient):
    """No operator may buy a worker for a plan the architect has not finished."""
    project_id = await _project(async_client, await _owner(async_client))
    _brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    task_id = await _planned_task(async_client, project_id, story_id, attempt)

    response = await async_client.post(
        DISPATCH_URL,
        json={
            "task_id": task_id,
            "overrides": [EngineeringDispatchRefusal.PRODUCT_BRIEF_NOT_ADMITTED.value],
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


@pytest.mark.asyncio
async def test_a_task_with_no_brief_dispatches_exactly_as_before(
    async_client: AsyncClient,
):
    """The regression that makes the default safe: nothing else changed.

    A task of a project with no Product Brief at all, and a task in a story with
    no brief, are both admitted the moment they are `todo` — which is what
    `dispatch_admitted` defaulting to true means in practice.
    """
    project_id = await _project(async_client, await _owner(async_client))
    loose = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "No story at all",
            "status": "todo",
        },
    )
    assert loose.status_code == HTTPStatus.CREATED, loose.text
    assert loose.json()["dispatch_admitted"] is True
    assert loose.json()["planning_attempt_id"] is None

    story_id = await _story(async_client, project_id)
    in_story = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "Story with no brief",
            "status": "todo",
            "story_id": story_id,
        },
    )
    assert in_story.status_code == HTTPStatus.CREATED, in_story.text
    assert in_story.json()["dispatch_admitted"] is True

    assert (await _decide(async_client, loose.json()["id"]))["outcome"] == (
        EngineeringDispatchOutcome.ADMITTED
    )
    assert (await _decide(async_client, in_story.json()["id"]))["outcome"] == (
        EngineeringDispatchOutcome.ADMITTED
    )


@pytest.mark.asyncio
async def test_work_added_after_the_admission_is_ordinary_work(async_client: AsyncClient):
    """Once the boundary is crossed it stays crossed for that story."""
    project_id = await _project(async_client, await _owner(async_client))
    brief_id, story_id, attempt = await _planned_brief(async_client, project_id)
    planned = await _planned_task(async_client, project_id, story_id, attempt)
    await _cover(async_client, brief_id, "r1", attempt, task_id=planned)
    await _cover(async_client, brief_id, "r2", attempt, returned_reason="dropped")
    await _admit(async_client, brief_id, attempt)

    later = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "fix",
            "title": "Found in QA",
            "status": "todo",
            "story_id": story_id,
        },
    )

    assert later.status_code == HTTPStatus.CREATED, later.text
    assert later.json()["dispatch_admitted"] is True


@pytest.mark.asyncio
async def test_a_task_cannot_be_moved_into_a_plan_that_is_still_being_built(
    async_client: AsyncClient,
):
    """Nothing would ever release it, so the move is refused rather than stranded."""
    project_id = await _project(async_client, await _owner(async_client))
    _brief_id, story_id, _attempt = await _planned_brief(async_client, project_id)
    outsider = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "Planned elsewhere",
            "status": "todo",
        },
    )
    assert outsider.status_code == HTTPStatus.CREATED, outsider.text

    moved = await async_client.patch(
        f"/api/tasks/{outsider.json()['id']}", json={"story_id": story_id}
    )

    assert moved.status_code == HTTPStatus.CONFLICT, moved.text


# --- confirmed content is never updated in place -------------------------------


@pytest.mark.asyncio
async def test_a_changed_brief_is_a_new_revision_not_an_edit(async_client: AsyncClient):
    """There is no update path, and confirmation refuses anything but what is stored."""
    project_id = await _project(async_client, await _owner(async_client))
    first = await async_client.post(
        f"{BRIEFS_URL}/",
        json={
            "project_id": project_id,
            "title": "Reading tracker",
            "content": _CONTENT,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )
    assert first.status_code == HTTPStatus.CREATED, first.text
    assert first.json()["revision"] == 1

    changed = {
        "summary": "A bot that tracks reading and lending",
        "must_requirements": _CONTENT["must_requirements"],
    }
    mismatch = await async_client.post(
        f"{BRIEFS_URL}/{first.json()['id']}/confirm",
        json={"request_id": f"conf-{uuid.uuid4().hex}", "content": changed},
    )
    assert mismatch.status_code == HTTPStatus.CONFLICT, mismatch.text

    second = await async_client.post(
        f"{BRIEFS_URL}/",
        json={
            "project_id": project_id,
            "title": "Reading tracker",
            "content": changed,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )
    assert second.status_code == HTTPStatus.CREATED, second.text
    assert second.json()["revision"] == 2
    # The first revision is untouched: an architect planning against it cannot
    # have the ground move under it.
    reread = await async_client.get(f"{BRIEFS_URL}/{first.json()['id']}")
    assert reread.json()["content"] == _CONTENT


@pytest.mark.asyncio
async def test_creating_the_same_brief_twice_returns_the_revision_it_opened(
    async_client: AsyncClient,
):
    """The creating caller's retry is idempotent, so a lost response is harmless."""
    project_id = await _project(async_client, await _owner(async_client))
    body = {
        "project_id": project_id,
        "title": "Reading tracker",
        "content": _CONTENT,
        "request_id": f"req-{uuid.uuid4().hex}",
    }
    first = await async_client.post(f"{BRIEFS_URL}/", json=body)
    second = await async_client.post(f"{BRIEFS_URL}/", json=body)

    assert second.json()["id"] == first.json()["id"]
    assert second.json()["revision"] == first.json()["revision"]


@pytest.mark.asyncio
async def test_an_unconfirmed_brief_cannot_be_planned(async_client: AsyncClient):
    """Planning against intent the user has not confirmed is planning against a draft."""
    project_id = await _project(async_client, await _owner(async_client))
    story_id = await _story(async_client, project_id)
    created = await async_client.post(
        f"{BRIEFS_URL}/",
        json={
            "project_id": project_id,
            "title": "Reading tracker",
            "content": _CONTENT,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )
    brief_id = created.json()["id"]

    bound = await async_client.post(f"{BRIEFS_URL}/{brief_id}/story", json={"story_id": story_id})
    assert bound.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, bound.text
    claim = await async_client.post(f"{BRIEFS_URL}/{brief_id}/planning-attempts/claim")
    assert claim.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, claim.text
