#!/usr/bin/env python3
"""Register the stand as its own deploy target.

The stand has no second machine to deploy the applications it generates, so it
deploys them onto itself, under a separate account (`stand-deploy`) with its own
key and its own `/opt/services`. The orchestrator reaches that account exactly as
it would reach any managed server.

It is registered as `ready`, not `pending_setup`, because the host is prepared by
hand rather than by the provisioner. That is deliberate: `provision_access.yml`
connects as root and rewrites the target's sshd_config and ufw — on a stand that
target is the very machine the orchestrator runs on, so provisioning would
rewrite its own SSH and firewall, including re-enabling root login. The cost is
that the provisioning path is not exercised on the stand; the deploy path is.

    INTERNAL_API_KEY=... ./scripts/register_stand_target.py --api-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

# Capacity is what the allocator is allowed to hand out, not what the box has:
# the orchestrator itself lives here and must keep its own room.
HTTP_BAD_REQUEST = 400  # what the API answers when the handle is already taken

DEFAULTS = {
    "handle": "stand-self",
    "host": "5wwb.l.time4vps.cloud",
    "public_ip": "212.24.101.230",
    "ssh_user": "stand-deploy",
    "capacity_cpu": 2,
    "capacity_ram_mb": 2048,
    "capacity_disk_mb": 20480,
}


def _request(url: str, key: str, method: str, payload: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310 — http(s) URL from the operator
        url,
        method=method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Internal-API-Key": key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--handle", default=DEFAULTS["handle"])
    parser.add_argument("--key-path", default=str(Path.home() / ".ssh" / "stand_target_ed25519"))
    args = parser.parse_args()

    internal_key = os.environ.get("INTERNAL_API_KEY")
    if not internal_key:
        print("INTERNAL_API_KEY is required.", file=sys.stderr)
        return 2

    private_key = Path(args.key_path).read_text(encoding="utf-8")

    payload = {
        **DEFAULTS,
        "handle": args.handle,
        # The raw key never lands in the row: the API encrypts it into ssh_key_enc.
        "ssh_key": private_key,
        # Managed means the orchestrator may deploy here. It says nothing about a
        # provider — with an empty Time4VPS allowlist the sync never touches this
        # row, so a hand-registered target cannot be flipped to `missing`.
        "is_managed": True,
        "status": "ready",
        "notes": "The stand itself. Prepared by hand; the provisioner never runs here.",
        "labels": {"contour": "stand"},
    }

    try:
        server = _request(f"{args.api_url}/api/servers/", internal_key, "POST", payload)
    except urllib.error.HTTPError as exc:
        if exc.code != HTTP_BAD_REQUEST:
            raise
        # Already registered: re-state the fields that matter instead of failing,
        # so a re-run after a stand rebuild converges rather than needing a delete.
        updates = {
            key: payload[key]
            for key in ("host", "public_ip", "ssh_user", "ssh_key", "status", "labels")
        }
        server = _request(
            f"{args.api_url}/api/servers/{args.handle}", internal_key, "PATCH", updates
        )

    print(f"{server['handle']}: status={server['status']} ssh_user={server['ssh_user']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
