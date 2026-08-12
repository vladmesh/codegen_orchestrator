#!/usr/bin/env python3
"""The one door out of a QA executor's network.

A QA executor container sits on an `internal` Docker network: it has no route to
the internet, to the deployment under test, or to anything else outside that
network. That is what makes "QA cannot write to the application" a fact about
the network rather than a rule in a prompt — but the assigned CLI still has to
reach its own model backend, or no QA run can happen at all.

This process is that one exception, and it is deliberately narrow:

* it speaks only HTTP `CONNECT`. There is no origin-form or absolute-form
  request handling here, so it cannot be used as a plain HTTP forward proxy and
  cannot carry a `POST` to anything;
* it opens a tunnel only to a `host:port` given on its command line, which the
  runtime builds from the assigned agent's model backend and nothing else. A
  `CONNECT` to the deployment's public URL is refused with `403` by the same
  code path that refuses any other host;
* it holds no credential. It is started per run, on that run's network, and
  removed with the run.

It runs inside a container whose image is the QA executor's own, so it is
stdlib-only on purpose: nothing may have to be installed for a QA run to start.
The runtime ships this file's own source into that container, so this module has
to stay importable (for its tests) and executable (as `python3 -c <source>`).
"""

from __future__ import annotations

import asyncio
import sys

LISTEN_PORT = 3128
DEFAULT_PORT = 443
# How long a client may take to send its CONNECT line before it is dropped.
HANDSHAKE_TIMEOUT = 30
# Longest a single tunnelled request line may be, so a client cannot make this
# process buffer without bound before the allowlist has had a say.
MAX_REQUEST_BYTES = 8192
RELAY_CHUNK = 65536


class Refused(Exception):
    """The client asked for something this proxy does not open."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_allowlist(entries: list[str]) -> frozenset[tuple[str, int]]:
    """Turn `HOST[:PORT]` command-line entries into the set this proxy opens.

    A bare host means port 443. An entry that is not a host and a port is a
    configuration error and must stop the proxy: a proxy that silently drops an
    entry it did not understand is a proxy nobody can reason about.
    """
    allowed: set[tuple[str, int]] = set()
    for entry in entries:
        host, _, port = entry.rpartition(":")
        if not host:
            host, port = entry, str(DEFAULT_PORT)
        if not host or not port.isdigit():
            raise ValueError(f"{entry!r} is not HOST or HOST:PORT")
        allowed.add((host.lower(), int(port)))
    if not allowed:
        raise ValueError("an egress proxy with an empty allowlist opens nothing; refusing to start")
    return frozenset(allowed)


def parse_connect(request_line: str) -> tuple[str, int]:
    """Return the `host, port` of a CONNECT line, or refuse.

    Anything that is not CONNECT — `GET http://…`, `POST /…`, a bare word — is
    refused here rather than handled, because handling it is exactly the forward
    proxying this boundary exists to not do.
    """
    parts = request_line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        method = parts[0].upper() if parts else "(empty)"
        raise Refused(
            "405 Method Not Allowed",
            f"{method} is not tunnelled here; this proxy speaks CONNECT only",
        )
    host, _, port = parts[1].rpartition(":")
    if not host or not port.isdigit():
        raise Refused("400 Bad Request", f"{parts[1]!r} is not HOST:PORT")
    return host.lower(), int(port)


def authorize(allowed: frozenset[tuple[str, int]], host: str, port: int) -> None:
    """Refuse every destination that is not the run's model backend."""
    if (host, port) not in allowed:
        raise Refused(
            "403 Forbidden",
            f"{host}:{port} is not a destination this QA run may reach; "
            f"the deployment under test is reachable only through the capability endpoint",
        )


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(RELAY_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _refuse(writer: asyncio.StreamWriter, refusal: Refused) -> None:
    body = refusal.detail.encode("utf-8", "replace")
    writer.write(
        f"HTTP/1.1 {refusal.status}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Connection: close\r\n\r\n".encode("latin-1")
        + body
    )
    try:
        await writer.drain()
    except OSError:
        pass
    writer.close()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allowed: frozenset[tuple[str, int]],
) -> None:
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HANDSHAKE_TIMEOUT)
    except asyncio.LimitOverrunError:
        await _refuse(writer, Refused("431 Request Header Fields Too Large", "header too large"))
        return
    except (TimeoutError, asyncio.IncompleteReadError, OSError):
        writer.close()
        return

    if len(header) > MAX_REQUEST_BYTES:
        await _refuse(writer, Refused("431 Request Header Fields Too Large", "header too large"))
        return

    request_line = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    try:
        host, port = parse_connect(request_line)
        authorize(allowed, host, port)
    except Refused as refusal:
        print(f"qa_egress_refused status={refusal.status} request={request_line!r}", flush=True)
        await _refuse(writer, refusal)
        return

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=HANDSHAKE_TIMEOUT
        )
    except (OSError, TimeoutError) as exc:
        print(f"qa_egress_upstream_failed host={host}:{port} error={exc}", flush=True)
        await _refuse(writer, Refused("502 Bad Gateway", f"{host}:{port} did not answer: {exc}"))
        return

    print(f"qa_egress_opened host={host}:{port}", flush=True)
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(
        _relay(reader, upstream_writer),
        _relay(upstream_reader, writer),
    )


async def serve(allowed: frozenset[tuple[str, int]], port: int = LISTEN_PORT) -> None:
    async def _client(reader, writer):
        try:
            await handle_client(reader, writer, allowed)
        except Exception as exc:  # noqa: BLE001 — one bad client must not close the door
            print(f"qa_egress_client_error error={exc}", flush=True)
            writer.close()

    server = await asyncio.start_server(_client, "0.0.0.0", port)  # noqa: S104
    print(
        f"qa_egress_listening port={port} allowed={','.join(sorted(f'{h}:{p}' for h, p in allowed))}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> int:
    allowed = parse_allowlist(argv)
    asyncio.run(serve(allowed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
