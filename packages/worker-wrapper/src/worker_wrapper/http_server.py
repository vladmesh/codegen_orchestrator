"""Lightweight HTTP server for worker result reporting and infra proxy.

Runs as an asyncio task alongside the agent subprocess. The agent calls:
- POST /result to report task results
- POST /infra/compose to manage infrastructure (proxied to worker-manager)

Uses only stdlib asyncio (no aiohttp/flask dependency).
"""

import asyncio
from collections.abc import Awaitable, Callable
from http import HTTPStatus
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError
import structlog

from shared.contracts.queues.worker_result import WorkerResult

from .http_models import (
    ResultRequest,
    to_worker_result,
)

logger = structlog.get_logger(__name__)


class ResultHttpServer:
    """Async HTTP server that accepts worker results and proxies infra commands.

    Args:
        worker_id: Worker identifier (for logging and infra proxy).
        publish_callback: Async callable that publishes a dict to Redis.
        result_event: Set when the first valid result is received.
        host: Bind address (default localhost).
        port: Bind port (0 = OS-assigned).
    """

    def __init__(
        self,
        worker_id: str,
        publish_callback: Callable[[WorkerResult], Awaitable[None]],
        result_event: asyncio.Event,
        host: str = "127.0.0.1",
        port: int = 9090,
    ):
        self.worker_id = worker_id
        self._publish = publish_callback
        self._result_event = result_event
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Actual port (useful when port=0 for OS-assigned)."""
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        logger.info(
            "http_result_server_started",
            worker_id=self.worker_id,
            host=self._host,
            port=self.port,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("http_result_server_stopped", worker_id=self.worker_id)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            status, body = await self._process_request(reader)
            await self._send_response(writer, status, body)
        except Exception:
            logger.exception("http_handler_error", worker_id=self.worker_id)
            await self._send_response(
                writer,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Internal server error"},
            )
        finally:
            writer.close()
            await writer.wait_closed()

    async def _process_request(self, reader: asyncio.StreamReader) -> tuple[HTTPStatus, dict]:
        # Read request line
        request_line = await reader.readline()
        if not request_line:
            return HTTPStatus.BAD_REQUEST, {"error": "Empty request"}

        parts = request_line.decode().strip().split(" ", 2)
        min_parts = 2
        if len(parts) < min_parts:
            return HTTPStatus.BAD_REQUEST, {"error": "Malformed request line"}

        method, path = parts[0], parts[1]

        # Read headers
        content_length = 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            header = line.decode().strip().lower()
            if header.startswith("content-length:"):
                content_length = int(header.split(":", 1)[1].strip())

        # Method check
        if method != "POST":
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Only POST allowed"}

        # Read body
        raw_body = await reader.readexactly(content_length) if content_length else b""

        # Route dispatch
        if path == "/result":
            return await self._handle_result(raw_body)
        elif path == "/infra/compose":
            return await self._handle_infra_compose(raw_body)
        else:
            return HTTPStatus.NOT_FOUND, {"error": f"Unknown path: {path}"}

    async def _handle_result(self, raw_body: bytes) -> tuple[HTTPStatus, dict]:
        """Handle POST /result — worker task result."""
        # Parse JSON
        try:
            data = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as e:
            return HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {e}"}

        # Check for duplicate result
        if self._result_event.is_set():
            return HTTPStatus.CONFLICT, {
                "error": "Result already submitted. Only one result per task."
            }

        # Validate with Pydantic model
        try:
            request = ResultRequest(**data)
        except ValidationError as e:
            errors = e.errors()
            return HTTPStatus.BAD_REQUEST, {"error": str(errors)}

        # Build the typed worker result and publish
        result = to_worker_result(request)

        logger.info(
            "http_result_received",
            worker_id=self.worker_id,
            success=request.success,
            status=result.status,
        )

        await self._publish(result)
        self._result_event.set()

        return HTTPStatus.OK, {"ok": True}

    async def _handle_infra_compose(self, raw_body: bytes) -> tuple[HTTPStatus, dict]:
        """Handle POST /infra/compose — proxy to worker-manager."""
        manager_url = os.environ.get("WORKER_MANAGER_URL")
        worker_id = os.environ.get("WORKER_ID")

        if not manager_url or not worker_id:
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "WORKER_MANAGER_URL or WORKER_ID not configured"
            }

        target_url = f"{manager_url}/api/worker/{worker_id}/infra/compose"

        # Run blocking urllib in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._proxy_request, target_url, raw_body)

    @staticmethod
    def _proxy_request(target_url: str, raw_body: bytes) -> tuple[HTTPStatus, dict]:
        """Forward request to worker-manager and return its response."""
        # target_url is always internal worker-manager, not user-controlled
        req = Request(  # noqa: S310
            target_url,
            data=raw_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=180) as resp:  # noqa: S310
                response_body = resp.read()
                return HTTPStatus.OK, json.loads(response_body)
        except HTTPError as e:
            try:
                error_body = json.loads(e.read())
            except (json.JSONDecodeError, AttributeError):
                error_body = {"error": str(e)}
            return HTTPStatus(e.code), error_body
        except URLError as e:
            logger.error("infra_proxy_connection_error", error=str(e))
            return HTTPStatus.BAD_GATEWAY, {"error": f"Cannot reach worker-manager: {e.reason}"}

    @staticmethod
    async def _send_response(writer: asyncio.StreamWriter, status: HTTPStatus, body: dict) -> None:
        payload = json.dumps(body).encode()
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + payload
        writer.write(response)
        await writer.drain()
