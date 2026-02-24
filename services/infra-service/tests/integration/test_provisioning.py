import pytest

from shared.contracts.queues.provisioner import ProvisionerMessage, ProvisionerResult


@pytest.mark.asyncio
async def test_provisioning_flow(mock_redis, mock_ansible_runner):
    """
    Test that process_provisioner_job processes a ProvisionerMessage
    and returns a ProvisionerResult with the correct status.
    """
    from src.main import process_provisioner_job

    # Setup input as plain dict (simulating what consume() yields after parsing)
    job_data = ProvisionerMessage(
        server_handle="droplet_123", force_reinstall=False, is_recovery=False
    ).model_dump(mode="json")

    # Mock successful ansible run
    mock_ansible_runner.run_playbook.return_value = (True, "Mock Success Output")

    # Process the job — returns ProvisionerResult
    result = await process_provisioner_job(job_data)

    assert isinstance(result, ProvisionerResult)
    assert result.server_handle == "droplet_123"
    assert result.status == "success"
