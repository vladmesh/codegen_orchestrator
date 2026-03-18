"""Tests for HTTP server integration with WorkerWrapper.

Tests that:
1. HTTP server starts before agent, stops after
2. Agent calling /complete via HTTP publishes result to output stream
3. Watchdog: agent exits without HTTP result → publishes failed
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

        async def fake_agent(data):
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
            status_line = response[: response.index(b"\r\n")].decode()
            assert "200" in status_line

        with patch.object(wrapper, "execute_agent", side_effect=fake_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_collect_and_archive"):
                                msg = MagicMock()
                                msg.message_id = "msg-1"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

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
            assert wrapper._http_server is not None
            assert wrapper._http_server.port > 0
            raise RuntimeError("Agent crashed")

        with patch.object(wrapper, "execute_agent", side_effect=failing_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_collect_and_archive"):
                                msg = MagicMock()
                                msg.message_id = "msg-2"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        assert wrapper._http_server is None or wrapper._http_server._server is None


class TestWatchdog:
    """When agent exits without HTTP result, publish failed."""

    async def test_watchdog_publishes_failed_when_no_http_result(self):
        """Agent exits without reporting via HTTP → auto-fail."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def silent_agent(data):
            pass  # Agent exits without calling HTTP

        with patch.object(wrapper, "execute_agent", side_effect=silent_agent):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_collect_and_archive"):
                                msg = MagicMock()
                                msg.message_id = "msg-3"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        redis_mock.publish.assert_any_call(
            "worker:test-w1:output",
            {"status": "failed", "error": "Agent exited without reporting result"},
        )

    async def test_http_result_prevents_watchdog(self):
        """If HTTP result received, watchdog does not publish failed."""
        config = _make_config()
        redis_mock = _make_redis_mock()
        wrapper = WorkerWrapper(config=config, redis_client=redis_mock)

        async def agent_with_http(data):
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

        with patch.object(wrapper, "execute_agent", side_effect=agent_with_http):
            with patch.object(wrapper, "publish_lifecycle", new_callable=AsyncMock):
                with patch.object(wrapper, "_git_pull", new_callable=AsyncMock):
                    with patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")):
                        with patch.object(wrapper, "_fix_venv_shebangs"):
                            with patch.object(wrapper, "_collect_and_archive"):
                                msg = MagicMock()
                                msg.message_id = "msg-5"
                                msg.data = {"prompt": "do stuff"}
                                await wrapper.process_message(msg)

        # HTTP result published by callback, no additional failed publish
        publish_calls = redis_mock.publish.call_args_list
        output_calls = [c for c in publish_calls if c[0][0] == "worker:test-w1:output"]
        assert len(output_calls) == 1
        assert output_calls[0][0][1]["commit_sha"] == "http-sha"
