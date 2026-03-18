"""Tests for the worker HTTP result server."""

import asyncio
from http import HTTPStatus
import json
from unittest.mock import AsyncMock

import pytest
from worker_wrapper.http_server import ResultHttpServer


@pytest.fixture
def publish_callback():
    return AsyncMock()


@pytest.fixture
def result_event():
    return asyncio.Event()


@pytest.fixture
async def server(publish_callback, result_event):
    srv = ResultHttpServer(
        worker_id="test-worker-123",
        publish_callback=publish_callback,
        result_event=result_event,
        host="127.0.0.1",
        port=0,  # OS-assigned port
    )
    await srv.start()
    yield srv
    await srv.stop()


def _url(server: ResultHttpServer, path: str) -> str:
    return f"http://127.0.0.1:{server.port}{path}"


async def _post(server: ResultHttpServer, path: str, body: dict) -> tuple[int, dict]:
    """Minimal HTTP POST using asyncio streams (no external deps)."""
    host = "127.0.0.1"
    port = server.port
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

    # Parse HTTP response
    header_end = response.index(b"\r\n\r\n")
    status_line = response[: response.index(b"\r\n")].decode()
    status_code = int(status_line.split(" ", 2)[1])
    response_body = response[header_end + 4 :]
    return status_code, json.loads(response_body)


class TestCompleteEndpoint:
    async def test_valid_complete(self, server, publish_callback, result_event):
        status, body = await _post(
            server,
            "/complete",
            {
                "commit": "abc123",
                "summary": "Added login endpoint",
            },
        )
        assert status == HTTPStatus.OK
        assert body["ok"] is True
        publish_callback.assert_awaited_once_with(
            {
                "status": "completed",
                "commit_sha": "abc123",
                "content": "Added login endpoint",
            }
        )
        assert result_event.is_set()

    async def test_missing_commit(self, server, publish_callback, result_event):
        status, body = await _post(server, "/complete", {"summary": "done"})
        assert status == HTTPStatus.BAD_REQUEST
        assert "error" in body
        publish_callback.assert_not_awaited()
        assert not result_event.is_set()

    async def test_empty_commit(self, server, publish_callback):
        status, body = await _post(
            server,
            "/complete",
            {
                "commit": "",
                "summary": "done",
            },
        )
        assert status == HTTPStatus.BAD_REQUEST

    async def test_duplicate_complete_rejected(self, server, publish_callback, result_event):
        await _post(
            server,
            "/complete",
            {
                "commit": "abc123",
                "summary": "first",
            },
        )
        status, body = await _post(
            server,
            "/complete",
            {
                "commit": "def456",
                "summary": "second",
            },
        )
        assert status == HTTPStatus.CONFLICT
        assert publish_callback.await_count == 1


class TestFailedEndpoint:
    async def test_valid_failed(self, server, publish_callback, result_event):
        status, body = await _post(
            server,
            "/failed",
            {
                "reason": "Tests don't pass",
            },
        )
        assert status == HTTPStatus.OK
        publish_callback.assert_awaited_once_with(
            {
                "status": "failed",
                "error": "Tests don't pass",
            }
        )
        assert result_event.is_set()

    async def test_missing_reason(self, server, publish_callback):
        status, body = await _post(server, "/failed", {})
        assert status == HTTPStatus.BAD_REQUEST


class TestBlockerEndpoint:
    async def test_valid_blocker(self, server, publish_callback, result_event):
        status, body = await _post(
            server,
            "/blocker",
            {
                "reason": "Need API key",
            },
        )
        assert status == HTTPStatus.OK
        publish_callback.assert_awaited_once_with(
            {
                "status": "blocked",
                "block_reason": "Need API key",
            }
        )
        assert result_event.is_set()

    async def test_missing_reason(self, server, publish_callback):
        status, body = await _post(server, "/blocker", {})
        assert status == HTTPStatus.BAD_REQUEST


class TestEdgeCases:
    async def test_unknown_path_returns_404(self, server):
        status, body = await _post(server, "/unknown", {})
        assert status == HTTPStatus.NOT_FOUND

    async def test_get_method_returns_405(self, server):
        """Only POST is allowed."""
        host = "127.0.0.1"
        port = server.port
        reader, writer = await asyncio.open_connection(host, port)
        request = (
            f"GET /complete HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(request)
        await writer.drain()
        response = await reader.read(65536)
        writer.close()
        await writer.wait_closed()
        status_line = response[: response.index(b"\r\n")].decode()
        status_code = int(status_line.split(" ", 2)[1])
        assert status_code == HTTPStatus.METHOD_NOT_ALLOWED

    async def test_invalid_json_returns_400(self, server):
        host = "127.0.0.1"
        port = server.port
        reader, writer = await asyncio.open_connection(host, port)
        payload = b"not json"
        request = (
            f"POST /complete HTTP/1.1\r\n"
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
        status_line = response[: response.index(b"\r\n")].decode()
        status_code = int(status_line.split(" ", 2)[1])
        assert status_code == HTTPStatus.BAD_REQUEST
