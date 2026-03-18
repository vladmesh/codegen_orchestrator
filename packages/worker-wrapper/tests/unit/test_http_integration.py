"""Tests for HTTP server integration with WorkerWrapper.

Tests that:
1. HTTP server starts before agent, stops after
2. Agent calling /complete via HTTP publishes result to output stream
3. Stdout parsing is skipped when HTTP result received
4. Watchdog: agent exits without HTTP result → falls back to stdout parsing
5. Watchdog: no HTTP result AND no stdout result → publishes failed
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


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

        # Mock execute_agent to simulate agent calling HTTP endpoint
        async def fake_agent(data):
            # Find the HTTP server port and call /complete
            port = wrapper._http_server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = json.dumps({"commit": "abc123", "summary": "Done"}).encode()
            request = (
                f"POST /complete HTTP/1.1\r\n"
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
            # Verify 200
            status_line = response[: response.index(b"\r\n")].decode()
            assert "200" in status_line

        with patch.object(wrapper, "execute_agent", side_effect=fake_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_inject_makefile_overrides"):
                                msg = MagicMock()
                                msg.message_id = "msg-1"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # The HTTP callback should have published to output stream
        redis_mock.publish.assert_any_call(
            "worker:test-w1:output",
            {"status": "completed", "commit_sha": "abc123", "content": "Done"},
        )

    async def test_http_server_stops_after_agent(self):
        """HTTP server is cleaned up even if agent fails."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def failing_agent(data):
            # Verify server is running
            assert wrapper._http_server is not None
            assert wrapper._http_server.port > 0
            raise RuntimeError("Agent crashed")

        with patch.object(wrapper, "execute_agent", side_effect=failing_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_inject_makefile_overrides"):
                                msg = MagicMock()
                                msg.message_id = "msg-2"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # Server should be stopped (no lingering server)
        assert wrapper._http_server is None or wrapper._http_server._server is None


class TestWatchdog:
    """When agent exits without HTTP result, fallback to stdout or fail."""

    async def test_no_http_no_stdout_publishes_failed(self):
        """Agent exits without reporting via HTTP or stdout → auto-fail."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        # execute_agent returns None (no stdout result) and no HTTP call
        async def silent_agent(data):
            return None

        with patch.object(wrapper, "execute_agent", side_effect=silent_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_inject_makefile_overrides"):
                                msg = MagicMock()
                                msg.message_id = "msg-3"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # Should publish failed to output stream
        redis_mock.publish.assert_any_call(
            "worker:test-w1:output",
            {"status": "failed", "error": "Agent exited without reporting result"},
        )

    async def test_stdout_result_used_as_fallback(self):
        """Agent produces stdout result but no HTTP call → stdout used."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def stdout_agent(data):
            # Return a parsed stdout result (backward compat path)
            return {"content": "did stuff", "status": "success", "commit_sha": "def456"}

        with patch.object(wrapper, "execute_agent", side_effect=stdout_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_inject_makefile_overrides"):
                                msg = MagicMock()
                                msg.message_id = "msg-4"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # Should publish the stdout result
        redis_mock.publish.assert_any_call(
            "worker:test-w1:output",
            {"content": "did stuff", "status": "success", "commit_sha": "def456"},
        )

    async def test_http_result_takes_priority_over_stdout(self):
        """If HTTP result received, stdout result is ignored."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def agent_with_both(data):
            # Call HTTP endpoint
            port = wrapper._http_server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = json.dumps({"commit": "http-sha", "summary": "HTTP result"}).encode()
            request = (
                f"POST /complete HTTP/1.1\r\n"
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
            # Also return stdout result
            return {"content": "stdout result", "status": "success", "commit_sha": "stdout-sha"}

        with patch.object(wrapper, "execute_agent", side_effect=agent_with_both):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_inject_makefile_overrides"):
                                msg = MagicMock()
                                msg.message_id = "msg-5"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # HTTP result should be published (by the server callback), NOT stdout
        # The stdout result should NOT be published separately
        publish_calls = redis_mock.publish.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        assert output_calls[0][0][1]["commit_sha"] == "http-sha"
