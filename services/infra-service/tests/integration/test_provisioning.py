import json

import pytest

from shared.contracts.queues.provisioner import ProvisionerMessage, ProvisionerResult


@pytest.mark.asyncio
async def test_provisioning_flow(mock_redis, mock_ansible_runner):
    """
    Test that the service consumes a ProvisionerMessage,
    runs the mocked ansible runner, and publishes a ProvisionerResult.
    """
    # 1. Setup Input Message
    msg = ProvisionerMessage(server_handle="droplet_123", force_reinstall=False, is_recovery=False)

    # 2. Publish to Redis (simulate Scheduler)
    await mock_redis.xadd("provisioner:queue", {"data": msg.model_dump_json()})

    # 3. Wait for processing (In a real test we'd run the service loop steps)
    # Since we can't easily run the infinite loop of main.py, we might need
    # to import the processor function or run main for a brief time.
    # For now, let's assume we are testing the `process_message` logic if we can import it,
    # OR we rely on the service being run via docker-compose in service tests.

    # HOWEVER, this file is being run INSIDE the container by `pytest`.
    # So we should probably import the logic we want to test.

    from src.main import process_provisioner_job

    # Mocking dependencies injected into main or available in global scope if any
    # But main.py likely instantiates clients.
    # Let's mock the keys required for process_message if needed.

    # Actually, simpler approach for "Service Test" (integration inside container):
    # We want to test the `ProvisionerNode` or similar class that handles logic.

    # Mock successful run
    mock_ansible_runner.run_playbook.return_value = (True, "Mock Success Output")

    # Manually trigger processing of the last message (since we don't have the loop running)
    # In a full "black box" service test, we would have the service running in background.
    # But here we are running "Service Level Integration Test" (mocking internals partly).

    # Let's read the message back to verify we put it in correctly
    streams = await mock_redis.xread({"provisioner:queue": "0-0"}, count=1)
    assert len(streams) == 1
    raw_data = streams[0][1][0][1]

    # Decode parsing logic similar to main.py
    if b"data" in raw_data:
        job_data = json.loads(raw_data[b"data"])
    else:
        # Fallback if no data key or already dict (unlikely with xadd)
        job_data = {k.decode(): v.decode() for k, v in raw_data.items()}

    await process_provisioner_job(job_data)

    # 4. Verify Result published
    results = await mock_redis.xread({"provisioner:results": "0-0"}, count=1)
    assert len(results) == 1

    _, result_data = results[0][1][0]
    result_json = result_data[b"data"].decode()
    result = ProvisionerResult.model_validate_json(result_json)

    assert result.server_handle == "droplet_123"
    assert result.status == "success"
