from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig


class MockProcess:
    def __init__(self, stdout, stderr, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr


@pytest.fixture
def wrapper_config():
    return WorkerWrapperConfig(
        redis_url="redis://localhost",
        input_stream="in",
        output_stream="out",
        consumer_group="grp",
        consumer_name="worker-1",
        agent_type="claude",
    )


@pytest.fixture
def fake_redis():
    return FakeAsyncRedis()


class TestWorkerWrapperComponent:
    @pytest.mark.asyncio
    async def test_full_execution_flow(self, wrapper_config, fake_redis):
        """
        Verify the full flow:
        1. Parse message
        2. Create session
        3. Build command
        4. Exec subprocess (mocked)
        5. Parse result
        """
        # Mock Redis client wrapper to return our fake redis
        mock_redis_client = MagicMock()
        mock_redis_client.redis = fake_redis

        wrapper = WorkerWrapper(config=wrapper_config, redis_client=mock_redis_client)

        # Test Data
        data = {"content": "Do something"}

        # Mock subprocess
        mock_stdout = b'Some logs\n<result>{"status": "success"}</result>'
        mock_process = MockProcess(stdout=mock_stdout, stderr=b"", returncode=0)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process

            result = await wrapper.execute_agent(data)

            # Verify result parsed
            assert result["status"] == "success"

            # Verify session created in Redis
            keys = await fake_redis.keys("worker:session:*")
            assert len(keys) == 1

            # Verify command structure (Claude)
            args = mock_exec.call_args[0]
            assert "claude" in args
            assert "-p" in args
            assert "Do something" in args

    @pytest.mark.asyncio
    async def test_handles_execution_failure(self, wrapper_config, fake_redis):
        """Should raise RuntimeError if subprocess fails."""
        mock_redis_client = MagicMock()
        mock_redis_client.redis = fake_redis
        wrapper = WorkerWrapper(config=wrapper_config, redis_client=mock_redis_client)

        mock_process = MockProcess(stdout=b"", stderr=b"Error occurred", returncode=1)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process

            with pytest.raises(RuntimeError) as exc:
                await wrapper.execute_agent({"content": "fail"})

            assert "Agent process failed" in str(exc.value)
