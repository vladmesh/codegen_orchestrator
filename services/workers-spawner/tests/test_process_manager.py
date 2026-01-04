"""Unit tests for ProcessManager."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from workers_spawner.process_manager import ProcessHandle, ProcessManager


class TestProcessHandle:
    """Tests for ProcessHandle dataclass."""

    def test_is_alive_when_running(self):
        """Process is alive when returncode is None."""
        mock_process = MagicMock()
        mock_process.returncode = None

        mock_factory = MagicMock()
        handle = ProcessHandle(
            process=mock_process,
            factory=mock_factory,
            agent_id="agent-123",
        )

        assert handle.is_alive is True

    def test_is_alive_when_exited(self):
        """Process is not alive when returncode is set."""
        mock_process = MagicMock()
        mock_process.returncode = 0

        mock_factory = MagicMock()
        handle = ProcessHandle(
            process=mock_process,
            factory=mock_factory,
            agent_id="agent-123",
        )

        assert handle.is_alive is False

    def test_started_at_is_set(self):
        """started_at is automatically set to current time."""
        mock_process = MagicMock()
        mock_factory = MagicMock()

        before = datetime.now(UTC)
        handle = ProcessHandle(
            process=mock_process,
            factory=mock_factory,
            agent_id="agent-123",
        )
        after = datetime.now(UTC)

        assert before <= handle.started_at <= after


class TestProcessManager:
    """Tests for ProcessManager."""

    @pytest.fixture
    def manager(self):
        """Create fresh ProcessManager for each test."""
        return ProcessManager()

    @pytest.fixture
    def mock_factory(self):
        """Create mock AgentFactory."""
        factory = MagicMock()
        factory.get_persistent_command.return_value = "claude --dangerously-skip-permissions"
        factory.format_message_for_stdin.side_effect = lambda msg: f"{msg}\n"
        return factory

    @pytest.mark.asyncio
    async def test_start_process_creates_subprocess(self, manager, mock_factory):
        """start_process creates docker exec subprocess."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await manager.start_process("agent-abc", mock_factory)

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]

            # Verify docker exec command structure
            assert call_args[0] == "docker"
            assert call_args[1] == "exec"
            assert "-i" in call_args
            assert "agent-abc" in call_args
            assert mock_factory.get_persistent_command.called

    @pytest.mark.asyncio
    async def test_start_process_stores_handle(self, manager, mock_factory):
        """start_process stores ProcessHandle in internal state."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            assert "agent-abc" in manager._processes
            handle = manager._processes["agent-abc"]
            assert handle.agent_id == "agent-abc"
            assert handle.factory == mock_factory

    @pytest.mark.asyncio
    async def test_start_process_ignores_duplicate(self, manager, mock_factory):
        """start_process does nothing if process already exists."""
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await manager.start_process("agent-abc", mock_factory)
            await manager.start_process("agent-abc", mock_factory)  # Second call

            # Should only create subprocess once
            assert mock_exec.call_count == 1

    @pytest.mark.asyncio
    async def test_write_to_stdin_sends_formatted_message(self, manager, mock_factory):
        """write_to_stdin formats and writes message to stdin."""
        mock_stdin = AsyncMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stdin = mock_stdin
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)
            await manager.write_to_stdin("agent-abc", "Hello world")

            mock_factory.format_message_for_stdin.assert_called_with("Hello world")
            mock_stdin.write.assert_called_with(b"Hello world\n")
            mock_stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_to_stdin_raises_if_not_found(self, manager):
        """write_to_stdin raises if agent not found."""
        with pytest.raises(RuntimeError, match="No process found"):
            await manager.write_to_stdin("nonexistent", "Hello")

    @pytest.mark.asyncio
    async def test_write_to_stdin_raises_if_dead(self, manager, mock_factory):
        """write_to_stdin raises if process is dead."""
        mock_process = AsyncMock()
        mock_process.returncode = 1  # Process exited
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            with pytest.raises(RuntimeError, match="is dead"):
                await manager.write_to_stdin("agent-abc", "Hello")

    @pytest.mark.asyncio
    async def test_read_stdout_returns_line(self, manager, mock_factory):
        """read_stdout_line returns decoded line."""
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"Some output\n")

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stdout = mock_stdout
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            with patch("asyncio.wait_for", return_value=b"Some output\n"):
                line = await manager.read_stdout_line("agent-abc")

                assert line == "Some output"

    @pytest.mark.asyncio
    async def test_read_stdout_returns_none_on_timeout(self, manager, mock_factory):
        """read_stdout_line returns None when no data available."""
        import asyncio

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stdout = AsyncMock()
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                line = await manager.read_stdout_line("agent-abc")
                assert line is None

    @pytest.mark.asyncio
    async def test_stop_process_terminates_gracefully(self, manager, mock_factory):
        """stop_process sends SIGTERM and waits."""
        mock_stdin = AsyncMock()
        mock_stdin.close = MagicMock()
        mock_stdin.wait_closed = AsyncMock()

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stdin = mock_stdin
        mock_process.pid = 12345
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            with patch("asyncio.wait_for", return_value=None):
                result = await manager.stop_process("agent-abc")

            assert result is True
            mock_process.terminate.assert_called_once()
            assert "agent-abc" not in manager._processes

    @pytest.mark.asyncio
    async def test_stop_process_kills_on_timeout(self, manager, mock_factory):
        """stop_process sends SIGKILL if graceful shutdown times out."""
        import asyncio

        mock_stdin = AsyncMock()
        mock_stdin.close = MagicMock()
        mock_stdin.wait_closed = AsyncMock()

        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.stdin = mock_stdin
        mock_process.pid = 12345
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await manager.start_process("agent-abc", mock_factory)

            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await manager.stop_process("agent-abc")

            assert result is True
            mock_process.kill.assert_called_once()

    def test_list_agents(self, manager):
        """list_agents returns all active agent IDs."""
        # Manually add some handles
        manager._processes["agent-1"] = MagicMock()
        manager._processes["agent-2"] = MagicMock()

        agents = manager.list_agents()
        assert set(agents) == {"agent-1", "agent-2"}

    def test_is_running_true_for_alive_process(self, manager):
        """is_running returns True for alive process."""
        mock_process = MagicMock()
        mock_process.returncode = None

        handle = ProcessHandle(
            process=mock_process,
            factory=MagicMock(),
            agent_id="agent-123",
        )
        manager._processes["agent-123"] = handle

        assert manager.is_running("agent-123") is True

    def test_is_running_false_for_dead_process(self, manager):
        """is_running returns False for dead process."""
        mock_process = MagicMock()
        mock_process.returncode = 0

        handle = ProcessHandle(
            process=mock_process,
            factory=MagicMock(),
            agent_id="agent-123",
        )
        manager._processes["agent-123"] = handle

        assert manager.is_running("agent-123") is False

    def test_is_running_false_for_unknown_agent(self, manager):
        """is_running returns False for unknown agent."""
        assert manager.is_running("nonexistent") is False
