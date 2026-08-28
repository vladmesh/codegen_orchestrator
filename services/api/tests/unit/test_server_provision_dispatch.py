from unittest.mock import AsyncMock

import pytest

from src.routers.servers import provision_server


@pytest.mark.asyncio
async def test_provision_request_publishes_the_existing_provisioner_message():
    server = object()
    database = type("Database", (), {"get": AsyncMock(return_value=server)})()
    redis = type("Redis", (), {"publish_message": AsyncMock()})()

    response = await provision_server("bitlaunch-71234", db=database, redis=redis)

    assert response["server_handle"] == "bitlaunch-71234"
    assert response["request_id"]
    redis.publish_message.assert_awaited_once()
    queue, message = redis.publish_message.await_args.args
    assert queue == "provisioner:queue"
    assert message.server_handle == "bitlaunch-71234"
