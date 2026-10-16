"""User endpoints serve stored creation and last activity as UTC instants with a designator."""

from datetime import UTC, datetime
import time
import uuid

import pytest
from sqlalchemy import update

from shared.models.user import User

STORED_LAST_SEEN = datetime.fromtimestamp(1_787_002_383.102772, UTC)
STORED_CREATED_AT = datetime.fromtimestamp(1_785_574_800, UTC)


def _instant(value: str) -> datetime:
    assert value.endswith(("Z", "+00:00")), value
    return datetime.fromisoformat(value)


def _telegram_id() -> int:
    return 880_000_000 + (uuid.uuid4().int % 100_000_000)


@pytest.fixture
def non_utc_process(monkeypatch):
    """Run the in-process API with a local zone far from UTC."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_user_reads_carry_utc_designator_and_unshifted_instant(async_client, db_session):
    created = await async_client.post(
        "/api/users/", json={"telegram_id": _telegram_id(), "username": "utc-reader"}
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    await db_session.execute(
        update(User)
        .where(User.id == user_id)
        .values(last_seen=STORED_LAST_SEEN, created_at=STORED_CREATED_AT)
    )
    await db_session.commit()

    detail = (await async_client.get(f"/api/users/{user_id}")).json()
    listed = next(
        user for user in (await async_client.get("/api/users/")).json() if user["id"] == user_id
    )

    for body in (detail, listed):
        assert _instant(body["last_seen"]) == STORED_LAST_SEEN
        assert _instant(body["created_at"]) == STORED_CREATED_AT


async def test_upsert_records_last_activity_as_now_in_utc_in_a_non_utc_process(
    async_client, non_utc_process
):
    telegram_id = _telegram_id()
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": "utc-upsert"}
    )
    assert created.status_code == 201, created.text

    before = datetime.now(UTC)
    upserted = await async_client.post(
        "/api/users/upsert", json={"telegram_id": telegram_id, "username": "utc-upsert"}
    )
    after = datetime.now(UTC)

    assert upserted.status_code == 200, upserted.text
    assert before <= _instant(upserted.json()["last_seen"]) <= after
    fetched = (await async_client.get(f"/api/users/by-telegram/{telegram_id}")).json()
    assert before <= _instant(fetched["last_seen"]) <= after
