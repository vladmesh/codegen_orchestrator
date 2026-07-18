from unittest.mock import AsyncMock, MagicMock, patch

from conftest import MockProcess
from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig

from shared.contracts.vocab import AgentType


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
        1. Create session
        2. Build command
        3. Exec subprocess (mocked)
        4. Capture session_id from output
        5. execute_agent returns None (results via HTTP only)
        """
        mock_redis_client = MagicMock()
        mock_redis_client.redis = fake_redis

        wrapper = WorkerWrapper(config=wrapper_config, redis_client=mock_redis_client)

        data = {"content": "Do something"}

        mock_stdout = b'{"type":"result","session_id":"test-session-123"}\nAgent finished work'
        mock_process = MockProcess(stdout=mock_stdout, stderr=b"", returncode=0)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process

            result = await wrapper.execute_agent(data)

            # execute_agent now returns None — results come via HTTP
            assert result is None

            # Verify session captured from output and saved to Redis
            keys = await fake_redis.keys("worker:session:*")
            assert len(keys) == 1

            session_value = await fake_redis.get(keys[0])
            if isinstance(session_value, bytes):
                session_value = session_value.decode()
            assert session_value == "test-session-123"

            # Verify command structure (Claude) — minimal -p, full task in TASK.md
            args = mock_exec.call_args[0]
            assert "claude" in args
            assert "-p" in args
            p_idx = args.index("-p")
            assert "TASK.md" in args[p_idx + 1]
            assert "Do something" not in args

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

    @pytest.mark.asyncio
    async def test_codex_exec_uses_workspace_sandbox_without_output_bridge(
        self, wrapper_config, fake_redis
    ):
        mock_redis_client = MagicMock()
        mock_redis_client.redis = fake_redis
        codex_config = WorkerWrapperConfig(
            **(wrapper_config.model_dump() | {"agent_type": AgentType.CODEX})
        )
        wrapper = WorkerWrapper(config=codex_config, redis_client=mock_redis_client)
        mock_process = MockProcess(
            stdout=b"transport output must not become a result",
            stderr=b"transport diagnostics must not become a result",
            returncode=0,
        )

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_process

            result = await wrapper.execute_agent({"content": "not part of the command"})

        assert result is None
        assert mock_exec.call_args.args[:4] == (
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
        )
        assert mock_exec.call_args.args[4:6] == (
            "--config",
            "sandbox_workspace_write.network_access=true",
        )
        assert "TASK.md" in mock_exec.call_args.args[6]
        assert "not part of the command" not in mock_exec.call_args.args[6]
        assert wrapper._agent_stdout_tail is None
