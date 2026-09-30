"""Real row locks, record identities, administrator copies, and retained deferrals."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
import uuid

import pytest

from shared.contracts.dto.owner_notification import OwnerNotification
from shared.models import Run, Story
from shared.notifications import AdminDeliveryResult
from src.routers import _owner_notice_settlement as routes


@pytest.fixture
def admins(monkeypatch):
    delivery = AsyncMock(return_value=AdminDeliveryResult(configured=1, succeeded=1))
    monkeypatch.setattr(routes, "deliver_to_admins", delivery)
    return delivery


@pytest.fixture(params=["run", "story"])
async def notice_source(request, async_client, db_session, _tasks_project):
    story_id = f"notice-{uuid.uuid4().hex[:12]}"
    run_id = f"deploy-{uuid.uuid4().hex[:12]}"
    notice = OwnerNotification(
        event="story_blocked",
        text="Work stopped",
        story_id=story_id,
        project_id=_tasks_project,
        terminal_status="waiting_human_review",
        state="delivered",
        owed_at=datetime.now(UTC) - timedelta(days=50),
        delivered_at=datetime.now(UTC),
    )
    story = Story(
        id=story_id,
        project_id=uuid.UUID(_tasks_project),
        title="Deferred notice",
        status="waiting_human_review",
        created_by="po",
    )
    if request.param == "story":
        story.owner_notification = notice.model_dump(mode="json")
    db_session.add(story)
    await db_session.flush()
    if request.param == "run":
        db_session.add(
            Run(
                id=run_id,
                project_id=uuid.UUID(_tasks_project),
                story_id=story_id,
                type="deploy",
                status="completed",
                run_metadata={"owner_notification": notice.model_dump(mode="json")},
            )
        )
    await db_session.commit()
    return {
        "story_id": story_id,
        "record": notice,
        "source": request.param,
        "source_id": story_id if request.param == "story" else run_id,
        "owed_at": notice.owed_at.isoformat(),
    }


def command(source, **fields):
    return {
        **{k: source[k] for k in ("source", "source_id", "owed_at")},
        "told_state": "suppressed",
        "reason": "Small things can wait",
        "suppressed_by": "user",
        **fields,
    }


def endpoint(source):
    return f"/api/stories/{source['story_id']}/owner-notifications/settlement"


async def write_delivery(client, source, record):
    if source["source"] == "run":
        return await client.patch(
            f"/api/runs/{source['source_id']}",
            json={"run_metadata": {"owner_notification": record}},
        )
    return await client.patch(f"/api/stories/{source['story_id']}/owner-notification", json=record)


@pytest.mark.asyncio
async def test_suppress_copy_retrieve_and_resolve(async_client, notice_source, admins):
    source = notice_source
    result = await async_client.post(endpoint(source), json=command(source))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "delivered"
    assert result.json()["told_state"] == "suppressed"
    admins.assert_awaited_once()
    copy = admins.call_args.args[0]
    for fact in (
        source["story_id"],
        "story_blocked",
        "Work stopped",
        "Small things can wait",
        "user",
    ):
        assert fact in copy
    listing = await async_client.get(
        "/api/stories/owner-notifications/deferred",
        params={"project_id": source["record"].project_id},
    )
    assert any(row["source_id"] == source["source_id"] for row in listing.json())
    resolved = await async_client.post(
        endpoint(source),
        json=command(source, told_state="told", suppressed_by=None, reason="Told on return"),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["told_at"] is not None
    listing = await async_client.get(
        "/api/stories/owner-notifications/deferred",
        params={"project_id": source["record"].project_id},
    )
    assert not any(row["source_id"] == source["source_id"] for row in listing.json())


@pytest.mark.asyncio
async def test_late_delivery_write_and_replacement_preserve_deferral(
    async_client, notice_source, admins
):
    source = notice_source
    assert (await async_client.post(endpoint(source), json=command(source))).status_code == 200
    record = source["record"].model_dump(mode="json")
    assert (await write_delivery(async_client, source, record)).status_code == 200
    listing = await async_client.get(f"/api/stories/{source['story_id']}/owner-notifications")
    assert listing.json()[0]["notification"]["told_state"] == "suppressed"
    record["owed_at"] = datetime.now(UTC).isoformat()
    record["state"] = "owed"
    assert (await write_delivery(async_client, source, record)).status_code == 200
    listing = await async_client.get(f"/api/stories/{source['story_id']}/owner-notifications")
    assert len(listing.json()) == 2
    assert listing.json()[1]["notification"]["told_state"] == "suppressed"
    stale = await async_client.post(
        endpoint(source),
        json=command(
            source,
            told_state="told",
            suppressed_by=None,
        ),
    )
    assert stale.status_code == 409

    closed = await async_client.post(
        endpoint(source),
        json=command(
            source,
            told_state="closed",
            suppressed_by=None,
            reason="User said drop it",
            resolve_deferred=True,
        ),
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["closed_at"] is not None


@pytest.mark.asyncio
async def test_stale_owed_at_is_refused_without_modifying_record(async_client, notice_source):
    stale = command(
        notice_source,
        told_state="told",
        suppressed_by=None,
        owed_at=(notice_source["record"].owed_at - timedelta(seconds=1)).isoformat(),
    )
    response = await async_client.post(endpoint(notice_source), json=stale)
    assert response.status_code == 409
    rows = (
        await async_client.get(f"/api/stories/{notice_source['story_id']}/owner-notifications")
    ).json()
    assert rows[0]["notification"]["told_state"] is None


@pytest.mark.asyncio
async def test_admin_decider_and_auth(async_client, notice_source, admins):
    # This Telegram identity is an administrator created by _tasks_project.
    response = await async_client.post(
        endpoint(notice_source),
        json=command(notice_source, suppressed_by="admin"),
        headers={"X-Telegram-ID": "999000999"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["suppressed_by"] == "admin"
    refused = await async_client.post(
        endpoint(notice_source), json=command(notice_source), headers={"X-Internal-Key": "invalid"}
    )
    assert refused.status_code in {401, 403}


@pytest.mark.asyncio
async def test_secret_refusal_and_nonempty_reason(async_client, notice_source, admins):
    source = notice_source
    record = source["record"].model_dump(mode="json")
    record.update(event="story_waiting_user_secret", terminal_status="waiting_user_secret")
    assert (await write_delivery(async_client, source, record)).status_code == 200
    response = await async_client.post(endpoint(source), json=command(source))
    assert response.status_code == 409
    assert "Only the user" in response.text
    response = await async_client.post(endpoint(source), json=command(source, reason=" "))
    assert response.status_code == 422
    admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_record_gone_before_the_copy_is_marked_is_a_clean_error(
    async_client, notice_source, db_session, monkeypatch
):
    """The deferral committed and the copy was sent; then the source row lost the record."""
    source = notice_source

    async def deliver_then_lose_the_record(*args, **kwargs):
        if source["source"] == "run":
            await db_session.delete(await db_session.get(Run, source["source_id"]))
        else:
            story = await db_session.get(Story, source["story_id"])
            story.owner_notification = None
        await db_session.commit()
        return AdminDeliveryResult(configured=1, succeeded=1)

    monkeypatch.setattr(routes, "deliver_to_admins", deliver_then_lose_the_record)

    result = await async_client.post(endpoint(source), json=command(source))

    assert result.status_code == 410, result.text
    assert "deferred and its administrator copy sent" in result.json()["detail"]
