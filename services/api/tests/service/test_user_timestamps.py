"""User endpoints return stored naive UTC timestamps as the same UTC instants."""

from datetime import UTC, datetime
import uuid

from sqlalchemy import update

from shared.models.user import User

STORED_LAST_SEEN = datetime(2026, 8, 27, 21, 33, 3, 102772)
STORED_CREATED_AT = datetime(2026, 8, 1, 9, 0, 0)


def _instant(value: str) -> datetime:
    assert value.endswith(("Z", "+00:00")), value
    return datetime.fromisoformat(value)


async def test_user_reads_carry_utc_designator_and_unshifted_instant(async_client, db_session):
    created = await async_client.post(
        "/api/users/",
        json={
            "telegram_id": 880_000_000 + (uuid.uuid4().int % 100_000_000),
            "username": "utc-reader",
        },
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
        assert _instant(body["last_seen"]) == STORED_LAST_SEEN.replace(tzinfo=UTC)
        assert _instant(body["created_at"]) == STORED_CREATED_AT.replace(tzinfo=UTC)
