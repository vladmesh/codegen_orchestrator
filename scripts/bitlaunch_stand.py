#!/usr/bin/env python3
"""Create, inspect and destroy the codegen stand host on BitLaunch.

The stand deploys into itself (see `register_stand_target.py`), so this owns
exactly one machine and no target. It is deliberately small: the VPS lifecycle
is the easy half of `issue:81da665c71b7728f8281`, and the hard half — delivering
Claude, Codex and Telethon credentials to the stand — is not here.

The API key is read from the environment or from an env file. It is never taken
from argv, never printed, and never written to an artifact.

    BITLAUNCH_API_KEY=... ./scripts/bitlaunch_stand.py list
    ./scripts/bitlaunch_stand.py create --wait
    ./scripts/bitlaunch_stand.py destroy --id <server_id> --yes
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://app.bitlaunch.io/api"

# BitLaunch's own KVM host. Cheapest 8 GiB on the account, and 8 GiB is the
# point: the stand exists to measure what two concurrent 4 GiB-capped coding
# workers actually peak at, and that number is only transferable if the stand
# has the same memory as production.
HOST_ID = 4
SIZE_ID = "nibble-8192"  # 4 vCPU, 8192 MB, 150 GB SSD
IMAGE_ID = "10006"  # Ubuntu 24.04 LTS — same major as the production target
REGION_ID = "ams3"  # Amsterdam 3; ams1 and ams2 carry no nibble sizes
SERVER_NAME = "codegen-stand"

CREATE_POLL_SECONDS = 10
CREATE_TIMEOUT_SECONDS = 600
HTTP_TOO_MANY_REQUESTS = 429


class BitLaunchError(RuntimeError):
    """An API call that did not answer the way the caller needs."""


def _api_key(env_file: Path | None) -> str:
    key = os.environ.get("BITLAUNCH_API_KEY", "").strip()
    if key:
        return key
    if env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "BITLAUNCH_API_KEY":
                key = value.strip().strip("\"'")
                if key:
                    return key
    raise BitLaunchError(
        "BITLAUNCH_API_KEY is not set and was not found in the env file. "
        "Never pass it on the command line."
    )


def _request(key: str, method: str, path: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — fixed https API root
        f"{API_ROOT}/{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        if exc.code == HTTP_TOO_MANY_REQUESTS:
            raise BitLaunchError("rate limited by BitLaunch; wait before retrying") from exc
        # A 500 on create with an otherwise valid body has one known cause: the
        # account's email address is unconfirmed, and provisioning is blocked
        # until it is. Say so instead of leaving the caller with "contact support".
        raise BitLaunchError(
            f"{method} {path} -> HTTP {exc.code}: {detail}\n"
            "If this is a create call, check `user` for emailConfirmed: an "
            "unconfirmed account fails provisioning with a generic 500."
        ) from exc


def _get_server(key: str, server_id: str) -> dict:
    """Read one server, unwrapping the envelope the single-server route adds.

    `GET /servers` answers with bare objects but `GET /servers/{id}` wraps its
    one in `{"server": {...}}`. Reading the top level of the wrapped form finds
    no status and no address, which looks exactly like a server that is still
    provisioning — a create that had already succeeded polled to its timeout.
    """
    payload = _request(key, "GET", f"servers/{server_id}")
    if not isinstance(payload, dict):
        raise BitLaunchError(f"unexpected response for server {server_id}")
    server = payload.get("server", payload)
    if not isinstance(server, dict):
        raise BitLaunchError(f"unexpected server envelope for {server_id}")
    return server


def cmd_account(key: str, _args: argparse.Namespace) -> int:
    user = _request(key, "GET", "user")
    assert isinstance(user, dict)
    print(
        json.dumps(
            {
                "email": user.get("email"),
                "emailConfirmed": user.get("emailConfirmed"),
                "balance_usd": round(user.get("balance", 0) / 100, 2),
                "servers_used": user.get("used"),
                "server_limit": user.get("limit"),
                "costPerHr": user.get("costPerHr"),
            },
            indent=1,
        )
    )
    if not user.get("emailConfirmed"):
        print("\nemailConfirmed is false — creating a server will fail with a 500.")
        return 1
    return 0


def cmd_list(key: str, _args: argparse.Namespace) -> int:
    servers = _request(key, "GET", "servers")
    assert isinstance(servers, list)
    if not servers:
        print("no servers on the account")
        return 0
    for server in servers:
        print(
            json.dumps(
                {k: server.get(k) for k in ("id", "name", "ipv4", "status", "created", "size")},
                indent=1,
            )
        )
    return 0


def cmd_create(key: str, args: argparse.Namespace) -> int:
    user = _request(key, "GET", "user")
    assert isinstance(user, dict)
    if not user.get("emailConfirmed"):
        print(
            "Refusing to create: the BitLaunch account's email is unconfirmed, so "
            "provisioning fails with an opaque 500. Confirm it first.",
            file=sys.stderr,
        )
        return 2

    keys = _request(key, "GET", "ssh-keys")
    assert isinstance(keys, dict)
    ssh_key_ids = [k["id"] for k in keys.get("keys", []) if k.get("name") == args.ssh_key_name]
    if not ssh_key_ids:
        raise BitLaunchError(
            f"no SSH key named {args.ssh_key_name!r} on the account; upload it before creating, "
            "so the stand is reachable the moment it boots"
        )

    created = _request(
        key,
        "POST",
        "servers",
        {
            "server": {
                "name": args.name,
                "hostID": HOST_ID,
                "hostImageID": IMAGE_ID,
                "sizeID": SIZE_ID,
                "regionID": REGION_ID,
                "sshKeys": ssh_key_ids,
            }
        },
    )
    assert isinstance(created, dict)
    server_id = created.get("id")
    print(f"created {server_id} ({args.name}, {SIZE_ID}, {REGION_ID})")

    if not args.wait:
        print("IP is not assigned until provisioning finishes; re-run `list` to read it.")
        return 0

    deadline = time.monotonic() + CREATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(CREATE_POLL_SECONDS)
        server = _get_server(key, str(server_id))
        ip = server.get("ipv4")
        print(f"  status={server.get('status')} ipv4={ip}")
        if ip:
            print(f"\nstand is up: root@{ip}")
            return 0
    print("timed out waiting for an IP; the server exists, read it with `list`", file=sys.stderr)
    return 1


def cmd_destroy(key: str, args: argparse.Namespace) -> int:
    server = _get_server(key, args.id)
    print(json.dumps({k: server.get(k) for k in ("id", "name", "ipv4", "status")}, indent=1))
    if not args.yes:
        print("\nRefusing to destroy without --yes. This is permanent.", file=sys.stderr)
        return 2
    _request(key, "DELETE", f"servers/{args.id}")
    print(f"destroyed {args.id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / "secretary" / ".env",
        help="fallback source for BITLAUNCH_API_KEY when it is not in the environment",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("account", help="show account state and whether it can provision")
    sub.add_parser("list", help="list servers on the account")

    create = sub.add_parser("create", help=f"create the stand ({SIZE_ID}, {REGION_ID})")
    create.add_argument("--name", default=SERVER_NAME)
    create.add_argument("--ssh-key-name", default="stands_ed25519")
    create.add_argument("--wait", action="store_true", help="poll until the IP is assigned")

    destroy = sub.add_parser("destroy", help="permanently destroy a server")
    destroy.add_argument("--id", required=True)
    destroy.add_argument("--yes", action="store_true", help="required; destruction is permanent")

    args = parser.parse_args()
    handlers = {
        "account": cmd_account,
        "list": cmd_list,
        "create": cmd_create,
        "destroy": cmd_destroy,
    }
    try:
        return handlers[args.command](_api_key(args.env_file), args)
    except BitLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
