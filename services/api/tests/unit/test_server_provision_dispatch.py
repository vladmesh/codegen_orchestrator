from unittest.mock import AsyncMock

import pytest

from src.routers.servers import ProvisioningRequest, provision_server


@pytest.mark.asyncio
async def test_provision_request_publishes_the_existing_provisioner_message():
    server = object()
    database = type("Database", (), {"get": AsyncMock(return_value=server)})()
    redis = type("Redis", (), {"publish_message": AsyncMock()})()

    response = await provision_server(
        "bitlaunch-6a920e74c9c98a452507b09b", db=database, redis=redis
    )

    assert response["server_handle"] == "bitlaunch-6a920e74c9c98a452507b09b"
    assert response["request_id"]
    redis.publish_message.assert_awaited_once()
    queue, message = redis.publish_message.await_args.args
    assert queue == "provisioner:queue"
    assert message.server_handle == "bitlaunch-6a920e74c9c98a452507b09b"


@pytest.mark.asyncio
async def test_stand_profile_is_an_explicit_queue_invocation_flag_not_a_server_label():
    server = object()
    database = type("Database", (), {"get": AsyncMock(return_value=server)})()
    redis = type("Redis", (), {"publish_message": AsyncMock()})()

    await provision_server(
        "bitlaunch-6a920e74c9c98a452507b09b",
        request=ProvisioningRequest(profile="stand_e2e"),
        db=database,
        redis=redis,
    )

    message = redis.publish_message.await_args.args[1]
    assert message.profile == "stand_e2e"
