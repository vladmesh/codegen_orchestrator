"""Tests for HTTP server integration with WorkerWrapper.

Tests that:
1. HTTP server starts before agent, stops after
2. Agent calling /complete via HTTP publishes result to output stream
3. Watchdog: agent exits without HTTP result → publishes failed
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper

from shared.contracts.queues.worker_result import (
    WorkerCompletedResult,
    WorkerResultStatus,
)


def _make_config(**overrides) -> WorkerWrapperConfig:
    defaults = {
        "redis_url": "redis://localhost:6379",
        "input_stream": "worker:test-w1:input",
        "output_stream": "worker:test-w1:output",
        "consumer_group": "test-group",
        "consumer_name": "test-w1",
        "agent_type": "claude",
        "subprocess_timeout_seconds": 10,
        "http_server_port": 0,  # OS-assigned
    }
    defaults.update(overrides)
    return WorkerWrapperConfig(**defaults)


def _make_redis_mock():
    redis_mock = AsyncMock()
    redis_mock.connect = AsyncMock()
    redis_mock.close = AsyncMock()
    redis_mock.publish = AsyncMock()
    redis_mock.publish_message = AsyncMock()
    redis_mock.redis = MagicMock()
    redis_mock.redis.hset = AsyncMock()
    return redis_mock


class TestHttpServerLifecycle:
    """HTTP server starts/stops around agent execution."""

    async def test_http_result_published_to_output_stream(self):
        """When agent POSTs to /complete, result appears on output stream."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def fake_agent(data):
            port = wrapper._http_server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = json.dumps({"success": True, "commit": "abc123", "summary": "Done"}).encode()
            request = (
                f"POST /result HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode() + payload
            writer.write(request)
            await writer.drain()
            response = await reader.read(65536)
            writer.close()
            await writer.wait_closed()
            status_line = response[: response.index(b"\r\n")].decode()
            assert "200" in status_line

        with patch.object(wrapper, "execute_agent", side_effect=fake_agent):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-1"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        redis_mock.publish_message.assert_any_call(
            "worker:test-w1:output",
            WorkerCompletedResult(commit_sha="abc123", content="Done"),
        )

    async def test_http_server_stops_after_agent(self):
        """HTTP server is cleaned up even if agent fails."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def failing_agent(data):
            assert wrapper._http_server is not None
            assert wrapper._http_server.port > 0
            raise RuntimeError("Agent crashed")

        with patch.object(wrapper, "execute_agent", side_effect=failing_agent):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-2"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        assert wrapper._http_server is None or wrapper._http_server._server is None


class TestStdoutCapture:
    """Agent stdout tail is captured and attached to results."""

    async def test_stdout_tail_attached_to_http_result(self):
        """When agent produces stdout, it's included in the published result."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def agent_with_stdout(data):
            # Simulate stdout capture
            wrapper._agent_stdout_tail = "Agent thinking about task..."
            # Also call HTTP result
            port = wrapper._http_server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = json.dumps({"success": True, "commit": "abc123", "summary": "Done"}).encode()
            request = (
                f"POST /result HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode() + payload
            writer.write(request)
            await writer.drain()
            await reader.read(65536)
            writer.close()
            await writer.wait_closed()

        with patch.object(wrapper, "execute_agent", side_effect=agent_with_stdout):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-stdout"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        publish_calls = redis_mock.publish_message.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        result = output_calls[0][0][1]
        assert result.agent_stdout_tail == "Agent thinking about task..."
        assert result.commit_sha == "abc123"

    async def test_stdout_tail_attached_to_error_result(self):
        """When agent crashes, stdout tail is still attached to failed result."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def crashing_agent(data):
            wrapper._agent_stdout_tail = "Partial output before crash"
            raise RuntimeError("Agent crashed")

        with patch.object(wrapper, "execute_agent", side_effect=crashing_agent):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-crash"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        publish_calls = redis_mock.publish_message.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        result = output_calls[0][0][1]
        assert result.status == WorkerResultStatus.FAILED
        assert result.agent_stdout_tail == "Partial output before crash"

    async def test_codex_diagnostics_are_not_persisted_or_returned(self):
        raw_diagnostic = "refresh-token-must-not-leak"
        config = _make_config(agent_type="codex")
        wrapper = WorkerWrapper(config=config, redis_client=_make_redis_mock())
        process = MagicMock(returncode=1)
        process.communicate = AsyncMock(
            return_value=(
                f"stdout {raw_diagnostic}".encode(),
                f"stderr {raw_diagnostic}".encode(),
            )
        )

        with patch(
            "worker_wrapper.session.SessionManager.get_or_create_session",
            new_callable=AsyncMock,
            return_value="unused",
        ):
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                spawn.return_value = process
                with pytest.raises(RuntimeError) as exc_info:
                    await wrapper.execute_agent({"prompt": "do stuff"})

        assert raw_diagnostic not in str(exc_info.value)
        assert wrapper._agent_stdout_tail is None


class TestWatchdog:
    """When agent exits without HTTP result, auto-resume or fail."""

    async def test_watchdog_publishes_failed_after_resume_fails(self):
        """Agent exits without HTTP → resume attempted → still no result → fail."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def silent_agent(data):
            pass  # Agent exits without calling HTTP

        async def resume_fails(data):
            return False  # Resume didn't help

        with patch.object(wrapper, "execute_agent", side_effect=silent_agent):
            with patch.object(wrapper, "_attempt_auto_resume", side_effect=resume_fails):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_paths"):
                            with patch.object(wrapper, "_collect_and_archive"):
                                msg = MagicMock()
                                msg.message_id = "msg-3"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        publish_calls = redis_mock.publish_message.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        assert output_calls[0][0][1].status == WorkerResultStatus.FAILED
        assert "without reporting result" in output_calls[0][0][1].error

    async def test_watchdog_skips_resume_for_non_claude(self):
        """Non-claude agents don't support resume — go straight to fail."""
        config = _make_config(agent_type="factory")
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def silent_agent(data):
            pass

        with patch.object(wrapper, "execute_agent", side_effect=silent_agent):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-factory"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        publish_calls = redis_mock.publish_message.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        assert output_calls[0][0][1].status == WorkerResultStatus.FAILED

    async def test_http_result_prevents_watchdog(self):
        """If HTTP result received, watchdog does not publish failed."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def agent_with_http(data):
            port = wrapper._http_server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = json.dumps(
                {"success": True, "commit": "http-sha", "summary": "HTTP result"}
            ).encode()
            request = (
                f"POST /result HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode() + payload
            writer.write(request)
            await writer.drain()
            await reader.read(65536)
            writer.close()
            await writer.wait_closed()

        with patch.object(wrapper, "execute_agent", side_effect=agent_with_http):
            with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                    with patch.object(wrapper, "_fix_venv_paths"):
                        with patch.object(wrapper, "_collect_and_archive"):
                            msg = MagicMock()
                            msg.message_id = "msg-5"
                            msg.data = {"prompt": "do stuff"}
                            await wrapper.process_message(msg)

        # HTTP result published by callback, no additional failed publish
        publish_calls = redis_mock.publish_message.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        assert output_calls[0][0][1].commit_sha == "http-sha"
