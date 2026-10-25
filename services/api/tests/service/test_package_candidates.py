"""Real PostgreSQL/Redis: a live story's DONE product-local INSTALL records one package candidate.

The completion transaction records it and owes administrators a notice; only admins read it.
"""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from test_local_install import buyer, local_install, post, product
from test_local_package_handoff import admitted_build, complete, fresh, success

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.package_candidate import PackageCandidateRead
from shared.models import PackageCandidate, Story, Task
from src.main import app

RATIONALE = "Several products summarise notes; one digest package serves them all."
OWNER_TEXT = "The story is finished. Tell the user the good news that their product is ready."
REVIEW_STOP = {"code": "no_new_commit", "source": "engineering", "detail": "Synthetic stop"}


async def built(client, db_session, owner=None, commit=None) -> dict:
    """A story whose package-build FEATURE handed off and whose INSTALL the executor settled."""
    owner = owner or await product(client, await buyer(client))
    build = await admitted_build(client, db_session, owner, description=RATIONALE)
    await complete(client, build, success(commit) if commit else None)
    install = await fresh(db_session, Task, build["child"])
    install.status = "done"
    await db_session.commit()
    return build


async def candidates(db_session, project_id: str) -> list[PackageCandidateRead]:
    rows = await db_session.scalars(
        select(PackageCandidate)
        .where(PackageCandidate.project_id == uuid.UUID(project_id))
        .execution_options(populate_existing=True)
    )
    return [PackageCandidateRead.model_validate(row) for row in rows.all()]


async def notice(client, story_id: str) -> OwnerNotification:
    response = await client.get(f"/api/stories/{story_id}/owner-notification")
    assert response.status_code == 200, response.text
    return OwnerNotification.model_validate(response.json())


def assert_candidate(candidate: PackageCandidateRead, build: dict, commit: str) -> None:
    owner = build["owner"]
    target = local_install(owner)
    assert candidate.model_dump(exclude={"id", "created_at", "updated_at"}) == {
        "project_id": uuid.UUID(owner["project"]),
        "story_id": owner["story"],
        "repository_id": owner["repository"],
        "source_task_id": build["source"],
        "install_task_id": build["child"],
        "path": target["path"],
        "package_name": target["package"]["name"],
        "package_distribution": target["package"]["distribution"],
        "package_version": target["package"]["version"],
        "commit": commit,
        "core_version": target["core_version"],
        "python_version": target["python_version"],
        "tooling_commit": target["tooling_commit"],
        "capability": "Generate notes digest",
        "rationale": RATIONALE,
    }


def assert_admin_notice(text: str, build: dict, commit: str) -> None:
    owner = build["owner"]
    for fact in (
        "Owned notes product",
        owner["project"],
        owner["story"],
        "notes-digest 0.1.0",
        "packages/notes_digest",
        commit,
        owner["repository"],
        "Generate notes digest",
        RATIONALE,
        "unreviewed and exclusive to this product",
        "personal code and security review before any release",
    ):
        assert fact in text


@pytest.mark.asyncio
async def test_completing_a_story_records_its_built_package_and_owes_the_admins(
    async_client, db_session
):
    build = await built(async_client, db_session)
    story_id = build["owner"]["story"]

    completed = await post(async_client, f"/api/stories/{story_id}/complete")

    assert completed["status"] == "completed"
    [candidate] = await candidates(db_session, build["owner"]["project"])
    assert_candidate(candidate, build, "1" * 40)
    record = await notice(async_client, story_id)
    assert (record.text, record.state, record.admin_state) == (
        OWNER_TEXT,
        OwnerNotificationState.OWED,
        OwnerNotificationState.OWED,
    )
    assert_admin_notice(record.admin_text, build, "1" * 40)


@pytest.mark.asyncio
async def test_accepting_a_reviewed_result_records_the_package_beside_the_owed_stop_notice(
    async_client, db_session
):
    build = await built(async_client, db_session)
    story_id = build["owner"]["story"]
    await post(async_client, f"/api/stories/{story_id}/human-review", {"failure": REVIEW_STOP})
    stop_notice = (await notice(async_client, story_id)).admin_text

    accepted = await post(
        async_client,
        f"/api/stories/{story_id}/accept-result",
        {"basis": "Reviewed the product"},
        headers={"X-Admin-Console-Operator": "candidate-fixture"},
    )

    assert accepted["status"] == "completed"
    [candidate] = await candidates(db_session, build["owner"]["project"])
    assert_candidate(candidate, build, "1" * 40)
    record = await notice(async_client, story_id)
    assert record.admin_state is OwnerNotificationState.OWED
    first, recorded = record.admin_text.split("\n\n")
    assert first == stop_notice
    assert_admin_notice(recorded, build, "1" * 40)


@pytest.mark.asyncio
async def test_a_story_without_a_built_package_records_nothing_and_owes_no_admin(
    async_client, db_session
):
    owner = await product(async_client, await buyer(async_client))

    await post(async_client, f"/api/stories/{owner['story']}/complete")

    assert await candidates(db_session, owner["project"]) == []
    record = await notice(async_client, owner["story"])
    assert (record.text, record.admin_text, record.admin_state) == (OWNER_TEXT, None, None)


@pytest.mark.asyncio
async def test_a_later_story_rebuilding_the_package_keeps_one_row_at_the_newest_commit(
    async_client, db_session
):
    first = await built(async_client, db_session)
    await post(async_client, f"/api/stories/{first['owner']['story']}/complete")
    [recorded] = await candidates(db_session, first["owner"]["project"])
    story = await post(
        async_client,
        "/api/stories/",
        {"project_id": first["owner"]["project"], "title": "Rebuild"},
        201,
    )
    await post(async_client, f"/api/stories/{story['id']}/start")
    second = await built(
        async_client, db_session, {**first["owner"], "story": story["id"]}, commit="3" * 40
    )

    await post(async_client, f"/api/stories/{story['id']}/complete")

    [rebuilt] = await candidates(db_session, first["owner"]["project"])
    assert (rebuilt.id, rebuilt.created_at) == (recorded.id, recorded.created_at)
    assert_candidate(rebuilt, second, "3" * 40)
    assert_admin_notice((await notice(async_client, story["id"])).admin_text, second, "3" * 40)


@pytest.mark.asyncio
async def test_an_unparseable_install_target_refuses_the_completion_naming_the_install(
    async_client, db_session
):
    build = await built(async_client, db_session)
    story_id = build["owner"]["story"]
    install = await fresh(db_session, Task, build["child"])
    install.install = {"route": "package_source"}
    await db_session.commit()
    before = (await fresh(db_session, Story, story_id)).owner_notification

    response = await async_client.post(f"/api/stories/{story_id}/complete")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "package_candidate_unrecordable",
        "task_id": build["child"],
        "cause": "install_target_invalid",
    }
    story = await fresh(db_session, Story, story_id)
    assert (story.status, story.owner_notification) == ("in_progress", before)
    assert await candidates(db_session, build["owner"]["project"]) == []


@pytest.mark.asyncio
async def test_only_an_administrator_reads_package_candidates(async_client, db_session):
    buyer_telegram = await buyer(async_client)
    build = await built(async_client, db_session, await product(async_client, buyer_telegram))
    await post(async_client, f"/api/stories/{build['owner']['story']}/complete")
    [candidate] = await candidates(db_session, build["owner"]["project"])
    admin = uuid.uuid4().int % 1_000_000_000
    await post(
        async_client,
        "/api/users/",
        {"telegram_id": admin, "username": "candidate-admin", "is_admin": True},
        201,
    )
    paths = ("/api/admin/v2/package-candidates", f"/api/admin/v2/package-candidates/{candidate.id}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        for path in paths:
            assert (await anonymous.get(path)).status_code == 401
    for path in paths:
        refused = await async_client.get(path, headers={"X-Telegram-ID": str(buyer_telegram)})
        assert refused.status_code == 403, refused.text

    headers = {"X-Telegram-ID": str(admin)}
    listed = await async_client.get(paths[0], headers=headers)
    assert listed.status_code == 200, listed.text
    assert candidate.model_dump(mode="json") in listed.json()
    read = await async_client.get(paths[1], headers=headers)
    assert PackageCandidateRead.model_validate(read.json()) == candidate
    missing = await async_client.get("/api/admin/v2/package-candidates/0", headers=headers)
    assert missing.status_code == 404
