#!/usr/bin/env python3
"""The one door out of a QA executor's network.

A QA executor container sits on an `internal` Docker network: it has no route to
the internet, to the platform's own services, or to anything else outside that
network. What the sandbox can reach is therefore exactly what this process
tunnels, and this process tunnels exactly three kinds of destination, all named
on its command line for one run:

* the assigned CLI's model backend, without which there is no executor;
* the host of the run's deployed public URL — the product under test — on the
  ports the runtime derived from that URL. Any request may be sent there,
  including a write: the owner accepted that the sandbox may change the product
  under test (product-data isolation is a later sprint), so the boundary is
  *where* the executor can go, not *what* it may say there;
* Telegram's MTProto data centres, as IP networks, so a Telethon client logged
  in as the QA account can talk to the bot under test.

The process is deliberately narrow:

* it speaks only HTTP `CONNECT`. There is no origin-form or absolute-form
  request handling here, so it cannot be used as a plain HTTP forward proxy; a
  plain-`http://` deployment is reached through a CONNECT tunnel
  (`curl --proxytunnel`, `http.client.HTTPConnection.set_tunnel`);
* it opens a tunnel only to a destination its allowlist names. A hostname entry
  matches that exact name; a network entry (`149.154.160.0/20:443`) matches a
  CONNECT to a literal IP inside it. A name is never resolved to be compared
  with a network, so a name cannot borrow a network entry;
* it holds no credential. It is started per run, on that run's network, and
  removed with the run.

It runs inside a container whose image is the QA executor's own, so it is
stdlib-only on purpose: nothing may have to be installed for a QA run to start.
The runtime ships this file's own source into that container, so this module has
to stay importable (for its tests) and executable (as `python3 -c <source>`).
"""

from __future__ import annotations

import asyncio
import ipaddress
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


Destination = str | ipaddress.IPv4Network | ipaddress.IPv6Network
Allowlist = frozenset[tuple[Destination, int]]


def _split_host_port(entry: str) -> tuple[str, str]:
    """`host`, `host:port`, `[v6]` or `[v6]:port` -> host and the port text ('' if none)."""
    if entry.startswith("["):
        host, bracket, rest = entry[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise ValueError(f"{entry!r} is not [HOST]:PORT")
        return host, rest[1:]
    if entry.count(":") > 1:
        # A bare IPv6 address or network, with no port.
        return entry, ""
    host, _, port = entry.rpartition(":")
    if not host:
        return entry, ""
    return host, port


def parse_allowlist(entries: list[str]) -> Allowlist:
    """Turn `HOST[:PORT]` and `NETWORK[:PORT]` command-line entries into the set this opens.

    A bare host or network means port 443. A network is written in CIDR form
    (`149.154.160.0/20:443`, `[2001:b28:f23d::/48]:443`) and is kept as a
    network, so a CONNECT to a literal IP inside it matches. An entry that is
    not a destination is a configuration error and must stop the proxy: a proxy
    that silently drops an entry it did not understand is a proxy nobody can
    reason about.
    """
    allowed: set[tuple[Destination, int]] = set()
    for entry in entries:
        host, port = _split_host_port(entry)
        port = port or str(DEFAULT_PORT)
        if not host or not port.isdigit():
            raise ValueError(f"{entry!r} is not HOST or HOST:PORT")
        destination: Destination = host.lower()
        if "/" in host:
            try:
                destination = ipaddress.ip_network(host)
            except ValueError as exc:
                raise ValueError(f"{entry!r} is not a network: {exc}") from exc
        allowed.add((destination, int(port)))
    if not allowed:
        raise ValueError("an egress proxy with an empty allowlist opens nothing; refusing to start")
    return frozenset(allowed)


_CONNECT_MIN_PARTS = 2


def parse_connect(request_line: str) -> tuple[str, int]:
    """Return the `host, port` of a CONNECT line, or refuse.

    Anything that is not CONNECT — `GET http://…`, `POST /…`, a bare word — is
    refused here rather than handled, because handling it is exactly the forward
    proxying this boundary exists to not do. An IPv6 host is returned without
    its brackets, ready to be dialled.
    """
    parts = request_line.split()
    if len(parts) < _CONNECT_MIN_PARTS or parts[0].upper() != "CONNECT":
        method = parts[0].upper() if parts else "(empty)"
        raise Refused(
            "405 Method Not Allowed",
            f"{method} is not tunnelled here; this proxy speaks CONNECT only",
        )
    try:
        host, port = _split_host_port(parts[1])
    except ValueError:
        host, port = "", ""
    if not host or not port.isdigit():
        raise Refused("400 Bad Request", f"{parts[1]!r} is not HOST:PORT")
    return host.lower(), int(port)


def authorize(allowed: Allowlist, host: str, port: int) -> None:
    """Refuse every destination that is not the run's backend, target or Telegram."""
    if (host, port) in allowed:
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        for destination, allowed_port in allowed:
            if (
                allowed_port == port
                and not isinstance(destination, str)
                and address.version == destination.version
                and address in destination
            ):
                return
    raise Refused(
        "403 Forbidden",
        f"{host}:{port} is not a destination this QA run may reach; the sandbox reaches "
        f"only its model backend, the deployment under test and Telegram",
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
    allowed: Allowlist,
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


async def serve(allowed: Allowlist, port: int = LISTEN_PORT) -> None:
    async def _client(reader, writer):
        try:
            await handle_client(reader, writer, allowed)
        except Exception as exc:  # noqa: BLE001 — one bad client must not close the door
            print(f"qa_egress_client_error error={exc}", flush=True)
            writer.close()

    # Proxy must accept its isolated executor network.
    server = await asyncio.start_server(_client, "0.0.0.0", port)  # noqa: S104
    print(
        f"qa_egress_listening port={port} "
        f"allowed={','.join(sorted(f'{h}:{p}' for h, p in allowed))}",
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
