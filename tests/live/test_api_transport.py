"""The live API clients survive a connection the server drops, and never resend a POST.

Run 36218333606 went red on `RemoteProtocolError: Server disconnected without
sending a response` from a deploy-run poll: uvicorn closed an idle keep-alive
connection at the instant the harness reused it. These drive the transport every
harness client is built with against a real local server — one that drops a
connection on its second request, which is that race made deterministic — and
not against a mock of httpx.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pipeline_helpers
import pytest
import uvicorn

pytestmark = pytest.mark.needs_no_api_credential

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DropOnReuseServer:
    """Answers the first request of every connection and drops the connection on the next.

    `requests` is what reached the server, as (connection number, method, answered).
    """

    requests: list[tuple[int, str, bool]] = field(default_factory=list)
    connections: int = 0

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        connection = self.connections
        served = 0
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                method = head.split(b" ", 1)[0].decode()
                length = 0
                for line in head.split(b"\r\n"):
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                if length:
                    await reader.readexactly(length)
                served += 1
                if served > 1:
                    self.requests.append((connection, method, False))
                    return
                self.requests.append((connection, method, True))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


@asynccontextmanager
async def drop_on_reuse_server() -> AsyncIterator[tuple[DropOnReuseServer, str]]:
    state = DropOnReuseServer()
    server = await asyncio.start_server(state.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield state, f"http://127.0.0.1:{port}"


@pytest.mark.parametrize(
    "factory",
    [
        pipeline_helpers.api_client_without_credentials,
        pipeline_helpers.api_client_as_test_user,
        pipeline_helpers.api_client_as_internal_service,
        pipeline_helpers.api_client_as_unscoped_observer,
        lambda **kwargs: pipeline_helpers.api_client_as_named_user(999000001, **kwargs),
    ],
    ids=["no-credentials", "test-user", "internal-service", "unscoped-observer", "named-user"],
)
async def test_a_get_on_a_dropped_connection_is_answered_on_a_fresh_one(monkeypatch, factory):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    async with drop_on_reuse_server() as (server, url), factory(base_url=url) as api:
        first = await api.get("/api/runs/")
        second = await api.get("/api/runs/")

    assert (first.status_code, second.status_code) == (200, 200)
    # The poll reused the pooled connection, lost it, and was sent once more on
    # a new one — exactly once.
    assert server.requests == [(1, "GET", True), (1, "GET", False), (2, "GET", True)]


async def test_a_post_on_a_dropped_connection_is_raised_and_never_resent():
    async with (
        drop_on_reuse_server() as (server, url),
        pipeline_helpers.api_client_without_credentials(base_url=url) as api,
    ):
        await api.get("/api/runs/")
        with pytest.raises(httpx.RemoteProtocolError):
            await api.post("/api/stories/", json={"title": "x"})

    # The server may have acted on the POST before it dropped the connection,
    # so the client sent it once and raised, rather than create a second story.
    assert server.requests == [(1, "GET", True), (1, "POST", False)]
    assert server.connections == 1


async def test_a_connection_idle_past_the_client_expiry_is_not_reused():
    """The race itself does not happen: an idle connection is forgotten before the server's."""
    async with (
        drop_on_reuse_server() as (server, url),
        pipeline_helpers.api_client_without_credentials(base_url=url) as api,
    ):
        await api.get("/api/runs/")
        await asyncio.sleep(pipeline_helpers.API_KEEPALIVE_EXPIRY_SECONDS + 0.2)
        await api.post("/api/stories/", json={"title": "x"})

    # A fresh connection, so nothing was dropped and nothing needed a retry —
    # a POST included.
    assert server.requests == [(1, "GET", True), (2, "POST", True)]


def test_the_client_expiry_is_below_the_keep_alive_the_api_really_runs_with():
    entrypoint = (REPO_ROOT / "services" / "api" / "entrypoint.sh").read_text()
    assert "uvicorn src.main:app" in entrypoint
    assert "--timeout-keep-alive" not in entrypoint, (
        "the API now sets its own keep-alive; API_SERVER_KEEPALIVE_SECONDS must follow it"
    )
    assert uvicorn.Config(app=None).timeout_keep_alive == (
        pipeline_helpers.API_SERVER_KEEPALIVE_SECONDS
    )
    assert pipeline_helpers.API_KEEPALIVE_EXPIRY_SECONDS < (
        pipeline_helpers.API_SERVER_KEEPALIVE_SECONDS / 2
    )


async def test_a_caller_supplied_transport_is_kept():
    """The offline doubles pass their own transport, and the factory must not replace it."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(204))
    async with pipeline_helpers.api_client_without_credentials(
        base_url="http://test", transport=transport
    ) as api:
        assert (await api.get("/health")).status_code == 204
