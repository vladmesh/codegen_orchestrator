"""Component test: HTTP server → Redis publish flow.

Tests the full path: agent POSTs to localhost HTTP → server validates →
callback publishes to Redis output stream. Uses AsyncMock for Redis
(component-level, not integration — no real Redis needed).
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from worker_wrapper.http_server import ResultHttpServer


async def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    """POST JSON to the server, return (status_code, response_body)."""
    host = "127.0.0.1"
    payload = json.dumps(body).encode()
    reader, writer = await asyncio.open_connection(host, port)
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
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
    header_end = response.index(b"\r\n\r\n")
    status_line = response[: response.index(b"\r\n")].decode()
    status_code = int(status_line.split(" ", 2)[1])
    response_body = response[header_end + 4 :]
    return status_code, json.loads(response_body)


class TestHttpToRedisFlow:
    """Full flow: HTTP POST → validate → publish callback."""

    @pytest.mark.asyncio
    async def test_complete_flow(self):
        publish = AsyncMock()
        event = asyncio.Event()
        server = ResultHttpServer(
            worker_id="w-001",
            publish_callback=publish,
            result_event=event,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            status, body = await _post(
                server.port,
                "/complete",
                {"commit": "a1b2c3d", "summary": "Implemented auth flow"},
            )
            assert status == 200
            assert body["ok"] is True
            publish.assert_awaited_once_with(
                {
                    "status": "completed",
                    "commit_sha": "a1b2c3d",
                    "content": "Implemented auth flow",
                }
            )
            assert event.is_set()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_failed_flow(self):
        publish = AsyncMock()
        event = asyncio.Event()
        server = ResultHttpServer(
            worker_id="w-002",
            publish_callback=publish,
            result_event=event,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            status, body = await _post(
                server.port,
                "/failed",
                {"reason": "Tests fail after 3 retries"},
            )
            assert status == 200
            publish.assert_awaited_once_with(
                {
                    "status": "failed",
                    "error": "Tests fail after 3 retries",
                }
            )
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_blocker_flow(self):
        publish = AsyncMock()
        event = asyncio.Event()
        server = ResultHttpServer(
            worker_id="w-003",
            publish_callback=publish,
            result_event=event,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            status, body = await _post(
                server.port,
                "/blocker",
                {"reason": "Need clarification on auth spec"},
            )
            assert status == 200
            publish.assert_awaited_once_with(
                {
                    "status": "blocked",
                    "block_reason": "Need clarification on auth spec",
                }
            )
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_validation_error_returns_400_with_detail(self):
        publish = AsyncMock()
        event = asyncio.Event()
        server = ResultHttpServer(
            worker_id="w-004",
            publish_callback=publish,
            result_event=event,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            # Missing required 'commit' field
            status, body = await _post(
                server.port,
                "/complete",
                {"summary": "done"},
            )
            assert status == 400
            assert "error" in body
            publish.assert_not_awaited()
            assert not event.is_set()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_second_result_rejected_with_409(self):
        publish = AsyncMock()
        event = asyncio.Event()
        server = ResultHttpServer(
            worker_id="w-005",
            publish_callback=publish,
            result_event=event,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            # First call succeeds
            s1, _ = await _post(
                server.port,
                "/complete",
                {"commit": "aaa", "summary": "first"},
            )
            assert s1 == 200

            # Second call rejected
            s2, body = await _post(
                server.port,
                "/failed",
                {"reason": "actually failed"},
            )
            assert s2 == 409
            assert publish.await_count == 1
        finally:
            await server.stop()
