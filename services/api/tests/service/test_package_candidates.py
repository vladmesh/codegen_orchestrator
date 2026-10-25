"""Real PostgreSQL/Redis: a live story's DONE product-local INSTALL records one package candidate.

The completion transaction records it and owes administrators a notice; only admins read it.
"""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from test_local_install import buyer, local_install, package_build, post, product
from test_local_package_handoff import admitted_build, complete, fresh, success

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.package_candidate import PackageCandidateRead
from shared.models import PackageCandidate, Story, Task
from shared.notifications import EMOJI_MAP
from shared.telegram_text import TELEGRAM_MESSAGE_LIMIT, utf16_length
from src.main import app

RATIONALE = "Several products summarise notes; one digest package serves them all."
OWNER_TEXT = "The story is finished. Tell the user the good news that their product is ready."
REVIEW_STOP = {"code": "no_new_commit", "source": "engineering", "detail": "Synthetic stop"}
REVIEW_WARNING = (
    "The code is unreviewed and exclusive to this product; it awaits the owner's "
    "personal code and security review before any release."
)


async def built(client, db_session, owner=None, commit=None, **source_changes) -> dict:
    """A story whose package-build FEATURE handed off and whose INSTALL the executor settled."""
    owner = owner or await product(client, await buyer(client))
    build = await admitted_build(
        client, db_session, owner, **{"description": RATIONALE, **source_changes}
    )
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


def candidate_line(candidate: PackageCandidateRead) -> str:
    return (
        f"{candidate.package_name} {candidate.package_version}, "
        f"commit {candidate.commit[:12]}: /package-candidates/{candidate.id}"
    )


def assert_admin_notice(text: str, build: dict, candidate: PackageCandidateRead) -> None:
    owner = build["owner"]
    header, line = text.split("\n")
    for fact in (
        "Owned notes product",
        owner["project"],
        owner["story"],
        "1 recorded",
        REVIEW_WARNING,
    ):
        assert fact in header
    assert line == candidate_line(candidate)


def outbound_length(admin_text: str) -> int:
    """The Telegram length of the admin text as the scheduler's error-level delivery sends it."""
    return utf16_length(f"{EMOJI_MAP['error']} {admin_text}")


def long_package(index: int) -> dict:
    """A package build with the longest admitted name and a long admitted version."""
    name = f"pkg{index:02d}-" + "n" * 58
    return {
        **package_build(),
        "path": f"packages/pkg_{index:02d}",
        "package": {
            "name": name,
            "distribution": f"codegen-kit-{name}",
            "version": f"{'9' * 240}.0.{index}",
        },
    }


async def twelve_built(client, db_session) -> dict:
    """One story with twelve distinct long-identity packages built and settled in order."""
    owner = await product(client, await buyer(client))
    for index in range(12):
        await built(
            client,
            db_session,
            owner,
            package_build=long_package(index),
            title=f"Generate package {index}",
        )
    return owner


def assert_twelve_notice(text: str, owner: dict, rows: list[PackageCandidateRead]) -> None:
    assert outbound_length(text) <= TELEGRAM_MESSAGE_LIMIT
    header, *listed, more = text.split("\n")
    for fact in ("Owned notes product", owner["project"], owner["story"], "12 recorded"):
        assert fact in header
    assert REVIEW_WARNING in header
    assert 0 < len(listed) < len(rows)
    assert listed == [candidate_line(row) for row in rows[: len(listed)]]
    assert more == f"+{len(rows) - len(listed)} more: /package-candidates"


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
    assert_admin_notice(record.admin_text, build, candidate)


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
    assert_admin_notice(recorded, build, candidate)


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
    assert_admin_notice((await notice(async_client, story["id"])).admin_text, second, rebuilt)


@pytest.mark.asyncio
async def test_an_unparseable_install_target_refuses_the_completion_naming_the_install(
    async_client, db_session
):
    build = await built(async_client, db_session)
    story_id = build["owner"]["story"]
    install = await fresh(db_session, Task, build["child"])
    target = install.install
    install.install = {"route": "package_source"}
    await db_session.commit()
    before = (await fresh(db_session, Story, story_id)).owner_notification
    try:
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
    finally:
        # Later tests list every task of the shared suite database as typed targets.
        install = await fresh(db_session, Task, build["child"])
        install.install = target
        await db_session.commit()


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


@pytest.mark.asyncio
async def test_a_long_rationale_stays_out_of_the_notice_and_whole_in_the_candidate(
    async_client, db_session
):
    owner = await product(async_client, await buyer(async_client))
    rationale = "digest " * 1430
    build = await built(async_client, db_session, owner, description=rationale)

    await post(async_client, f"/api/stories/{owner['story']}/complete")

    [candidate] = await candidates(db_session, owner["project"])
    assert candidate.rationale == rationale
    assert_candidate(candidate.model_copy(update={"rationale": RATIONALE}), build, "1" * 40)
    text = (await notice(async_client, owner["story"])).admin_text
    assert outbound_length(text) <= TELEGRAM_MESSAGE_LIMIT
    assert_admin_notice(text, build, candidate)


@pytest.mark.asyncio
async def test_a_package_with_the_longest_path_and_capability_completes_within_one_message(
    async_client, db_session
):
    path = "/".join(["s" * 127] * 7 + ["s" * 128])
    capability = "c" * 500
    build = await built(
        async_client,
        db_session,
        package_build={**package_build(), "path": path},
        title=capability,
    )

    await post(async_client, f"/api/stories/{build['owner']['story']}/complete")

    [candidate] = await candidates(db_session, build["owner"]["project"])
    assert (len(candidate.path), candidate.path, candidate.capability) == (1024, path, capability)
    text = (await notice(async_client, build["owner"]["story"])).admin_text
    assert outbound_length(text) <= TELEGRAM_MESSAGE_LIMIT
    assert_admin_notice(text, build, candidate)


@pytest.mark.asyncio
async def test_twelve_packages_record_every_row_and_the_notice_lists_what_fits(
    async_client, db_session
):
    owner = await twelve_built(async_client, db_session)

    completed = await post(async_client, f"/api/stories/{owner['story']}/complete")

    assert completed["status"] == "completed"
    rows = sorted(await candidates(db_session, owner["project"]), key=lambda row: row.id)
    assert [row.package_name for row in rows] == [
        long_package(index)["package"]["name"] for index in range(12)
    ]
    assert_twelve_notice((await notice(async_client, owner["story"])).admin_text, owner, rows)


@pytest.mark.asyncio
async def test_twelve_packages_after_a_long_stop_notice_fit_one_message_on_accept_result(
    async_client, db_session
):
    owner = await twelve_built(async_client, db_session)
    story_id = owner["story"]
    long_stop = {**REVIEW_STOP, "detail": "Synthetic stop detail. " * 30}
    await post(async_client, f"/api/stories/{story_id}/human-review", {"failure": long_stop})
    stop_notice = (await notice(async_client, story_id)).admin_text

    await post(
        async_client,
        f"/api/stories/{story_id}/accept-result",
        {"basis": "Reviewed the product"},
        headers={"X-Admin-Console-Operator": "candidate-fixture"},
    )

    rows = sorted(await candidates(db_session, owner["project"]), key=lambda row: row.id)
    assert len(rows) == 12
    text = (await notice(async_client, story_id)).admin_text
    assert outbound_length(text) <= TELEGRAM_MESSAGE_LIMIT
    carried, recorded = text.split("\n\n")
    assert carried == stop_notice
    assert_twelve_notice(recorded, owner, rows)


@pytest.mark.asyncio
async def test_candidate_notice_keeps_its_block_when_carried_admin_text_is_too_long(
    async_client, db_session
):
    build = await built(async_client, db_session)
    story_id = build["owner"]["story"]
    await post(async_client, f"/api/stories/{story_id}/human-review", {"failure": REVIEW_STOP})
    story = await fresh(db_session, Story, story_id)
    notification = dict(story.owner_notification)
    notification["admin_text"] = "Earlier administrator context. " * 500
    story.owner_notification = notification
    await db_session.commit()

    await post(
        async_client,
        f"/api/stories/{story_id}/accept-result",
        {"basis": "Reviewed the product"},
        headers={"X-Admin-Console-Operator": "candidate-fixture"},
    )

    [candidate] = await candidates(db_session, build["owner"]["project"])
    text = (await notice(async_client, story_id)).admin_text
    assert outbound_length(text) <= TELEGRAM_MESSAGE_LIMIT
    carried, recorded = text.split("\n\n")
    assert carried.endswith(f" [earlier administrator text for story {story_id} truncated]")
    assert_admin_notice(recorded, build, candidate)
