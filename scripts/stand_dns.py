#!/usr/bin/env python3
"""Give one ephemeral stand a resolvable name, and take it back afterwards.

A stand has only an IP, and a certificate cannot be issued for one: the client
that pushes the generated project's images is a GitHub runner, whose trust store
is not ours to add to, so the registry has to answer for a publicly trusted
certificate. That needs a name, so the run creates one and Caddy takes it from
there.

The name is derived from the run tag alone. Cleanup therefore needs no artifact
from the run it is cleaning: a run that died before publishing anything still has
its record removed by a job that knows only its own tag.

Records are never proxied. The point of the name is that the stand terminates TLS
itself, exactly as production does.

    CLOUDFLARE_API_TOKEN=... ./scripts/stand_dns.py create --run-tag gha-1-1 --ip 1.2.3.4
    CLOUDFLARE_API_TOKEN=... ./scripts/stand_dns.py cleanup --run-tag gha-1-1
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.cloudflare.com/client/v4"
DEFAULT_ZONE = "vladmesh.dev"
DEFAULT_SUBDOMAIN = "stand"
# Short enough that a replaced record is not served from a resolver's cache for
# longer than a stand lives.
RECORD_TTL_SECONDS = 60
RESOLVE_TIMEOUT_SECONDS = 300
RESOLVE_POLL_SECONDS = 5


class StandDNSError(RuntimeError):
    """A DNS operation that did not answer the way the caller needs."""


def _token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        raise StandDNSError("CLOUDFLARE_API_TOKEN is not set")
    return token


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — fixed https API root
        f"{API_ROOT}/{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # The body carries Cloudflare's reason; the status alone does not.
        detail = error.read().decode()[:400]
        raise StandDNSError(f"{method} {path} failed with {error.code}: {detail}") from error
    if not payload.get("success"):
        raise StandDNSError(f"{method} {path} was refused: {payload.get('errors')}")
    return payload


RUN_TAG = re.compile(r"^gha-\d+-\d+$")


def record_name(run_tag: str, *, zone: str, subdomain: str) -> str:
    """The one name this run answers to.

    The tag shape is enforced because these operations delete: a caller that
    passes something else could otherwise name, and remove, any record nested
    under the stand subdomain. The machines beside it are protected by a
    fail-closed ownership policy, and their name should not be weaker.
    """
    if not RUN_TAG.match(run_tag):
        raise StandDNSError(f"{run_tag!r} is not a stand run tag (gha-<run-id>-<attempt>)")
    return f"{run_tag}.{subdomain}.{zone}"


def _zone_id(zone: str) -> str:
    payload = _request("GET", f"zones?name={zone}")
    results = payload.get("result") or []
    if not results:
        raise StandDNSError(f"the token cannot see a zone named {zone}")
    return results[0]["id"]


def _existing_record_ids(zone_id: str, name: str) -> list[str]:
    payload = _request("GET", f"zones/{zone_id}/dns_records?type=A&name={name}")
    return [record["id"] for record in payload.get("result") or []]


def cmd_create(args: argparse.Namespace) -> int:
    name = record_name(args.run_tag, zone=args.zone, subdomain=args.subdomain)
    zone_id = _zone_id(args.zone)
    # A retried run reuses its tag, so replace rather than collide.
    for record_id in _existing_record_ids(zone_id, name):
        _request("DELETE", f"zones/{zone_id}/dns_records/{record_id}")
    _request(
        "POST",
        f"zones/{zone_id}/dns_records",
        {
            "type": "A",
            "name": name,
            "content": args.ip,
            "ttl": RECORD_TTL_SECONDS,
            "proxied": False,
        },
    )
    print(json.dumps({"name": name, "ip": args.ip}))
    return 0


def cmd_await(args: argparse.Namespace) -> int:
    """Block until the name resolves to the address the record names.

    Caddy asks for a certificate as soon as the stack starts, and an issuer that
    cannot resolve the name yet spends one of the week's attempts on a failure.
    """
    name = record_name(args.run_tag, zone=args.zone, subdomain=args.subdomain)
    deadline = time.monotonic() + RESOLVE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            resolved = {info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET)}
        except socket.gaierror:
            resolved = set()
        if args.ip in resolved:
            print(f"{name} resolves to {args.ip}")
            return 0
        time.sleep(RESOLVE_POLL_SECONDS)
    print(f"{name} did not resolve to {args.ip} in time", file=sys.stderr)
    return 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    name = record_name(args.run_tag, zone=args.zone, subdomain=args.subdomain)
    zone_id = _zone_id(args.zone)
    removed = []
    for record_id in _existing_record_ids(zone_id, name):
        _request("DELETE", f"zones/{zone_id}/dns_records/{record_id}")
        removed.append(record_id)
    print(json.dumps({"name": name, "removed_ids": removed}))
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Remove records older than the machines they name could possibly be.

    Machines expire on their own TTL sweep; their names have to go with them.
    A retained run that is never cleaned up by hand would otherwise leave a
    hostname pointing at an address the provider reassigns to somebody else.
    """
    zone_id = _zone_id(args.zone)
    suffix = f".{args.subdomain}.{args.zone}"
    cutoff = datetime.now(UTC) - timedelta(hours=args.ttl_hours)
    payload = _request("GET", f"zones/{zone_id}/dns_records?type=A&per_page=100")
    removed = []
    for record in payload.get("result") or []:
        name = record.get("name") or ""
        created = record.get("created_on")
        label = name[: -len(suffix)] if name.endswith(suffix) else ""
        # Age alone is not ownership: only a record this lifecycle could have
        # created is a record it may remove.
        if not label or not RUN_TAG.match(label) or not created:
            continue
        if datetime.fromisoformat(created.replace("Z", "+00:00")) >= cutoff:
            continue
        _request("DELETE", f"zones/{zone_id}/dns_records/{record['id']}")
        removed.append(name)
    print(json.dumps({"removed": removed}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zone", default=os.environ.get("STAND_DNS_ZONE", DEFAULT_ZONE))
    parser.add_argument(
        "--subdomain", default=os.environ.get("STAND_DNS_SUBDOMAIN", DEFAULT_SUBDOMAIN)
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "await", "cleanup"):
        item = sub.add_parser(command)
        item.add_argument("--run-tag", required=True)
    sweep = sub.add_parser("sweep")
    sweep.add_argument("--ttl-hours", type=int, required=True)
    sub.choices["create"].add_argument("--ip", required=True)
    sub.choices["await"].add_argument("--ip", required=True)
    args = parser.parse_args()

    handlers = {
        "create": cmd_create,
        "await": cmd_await,
        "cleanup": cmd_cleanup,
        "sweep": cmd_sweep,
    }
    try:
        return handlers[args.command](args)
    except StandDNSError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
