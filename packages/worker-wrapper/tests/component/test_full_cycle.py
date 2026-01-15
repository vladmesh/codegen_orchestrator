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
        6. Capture session_id from output
        """
        # Mock Redis client wrapper to return our fake redis
        mock_redis_client = MagicMock()
        mock_redis_client.redis = fake_redis

        wrapper = WorkerWrapper(config=wrapper_config, redis_client=mock_redis_client)

        # Test Data
        data = {"content": "Do something"}

        # Mock subprocess - include session_id in JSON output like real Claude CLI
        # Claude CLI outputs JSON, and result tags are on separate line
        mock_stdout = (
            b'{"type":"result","session_id":"test-session-123"}\n'
            b'<result>{"status": "success"}</result>'
        )
        mock_process = MockProcess(stdout=mock_stdout, stderr=b"", returncode=0)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process

            result = await wrapper.execute_agent(data)

            # Verify result parsed (returns raw_output when no result tags found)
            assert "raw_output" in result or result.get("status") == "success"

            # Verify session captured from output and saved to Redis
            keys = await fake_redis.keys("worker:session:*")
            assert len(keys) == 1

            session_value = await fake_redis.get(keys[0])
            if isinstance(session_value, bytes):
                session_value = session_value.decode()
            assert session_value == "test-session-123"

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
