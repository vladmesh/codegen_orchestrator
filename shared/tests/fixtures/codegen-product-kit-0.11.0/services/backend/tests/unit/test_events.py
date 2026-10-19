import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from faststream.redis import TestRedisBroker
import pytest

from shared.generated.events import get_broker, publish_user_granted
from shared.generated.schemas import Status, UserAccess


@pytest.mark.asyncio
async def test_publish_user_granted() -> None:
    async with TestRedisBroker(get_broker()):
        event = UserAccess(
            user_id=123,
            status=Status("active"),
            channel="telegram",
            external_id="456",
        )

        envelope = await publish_user_granted(event)

        assert envelope.payload == event
        assert envelope.event_id
        assert envelope.occurred_at.tzinfo is not None
        assert envelope.schema_version == 1
