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

# Capacity is the number the allocator does arithmetic against, not a limit the
# host enforces. A project is admitted when
#   capacity_ram_mb >= used_ram_mb + min_ram_mb + allocation_ram_reserve_mb
# which for a default project is used + 768 MB. The orchestrator itself occupies
# about 1.8 GB of this box's 3.8 GB, so the 2048 that looked conservative could
# never admit anything: every task parked on insufficient_free_memory while the
# machine sat idle. The honest number is most of the machine, with the swap file
# behind it absorbing what a worker adds during a run.
HTTP_BAD_REQUEST = 400  # what the API answers when the handle is already taken


def _env_int(name: str, fallback: int) -> int:
    """Read a capacity override, falling back rather than failing on nonsense."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {fallback}", file=sys.stderr)
        return fallback


# The stand is its own deploy target, so its address is the orchestrator's own —
# read from the environment rather than pinned to a machine. A stand is
# rebuilt and replaced; a hardcoded address silently registers the previous
# stand, and 5wwb, the address this held before, is a production target today.
DEFAULTS = {
    "handle": "stand-self",
    "host": os.environ.get("ORCHESTRATOR_HOSTNAME", ""),
    "public_ip": os.environ.get("ORCHESTRATOR_PUBLIC_IP", ""),
    "ssh_user": "stand-deploy",
    "capacity_cpu": _env_int("STAND_TARGET_CPU", 2),
    # Capacity is arithmetic for the allocator, not a limit the host enforces:
    # a project is admitted when capacity >= used + min_ram + reserve. Leave the
    # orchestrator's own footprint out of it, or nothing is ever admitted.
    "capacity_ram_mb": _env_int("STAND_TARGET_RAM_MB", 3400),
    "capacity_disk_mb": _env_int("STAND_TARGET_DISK_MB", 20480),
}


def _request(url: str, key: str, method: str, payload: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310 — http(s) URL from the operator
        url,
        method=method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Internal-Key": key},
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
        # `provisioning_phase` is what admission reads (shared/server_admission.py):
        # a server without it is never allocated, and the provisioner is normally
        # its only writer. The stand does not run the provisioner, so the label
        # states here what the playbook would have recorded — the host has docker,
        # the deploy account, /opt/services and the monitoring baseline the health
        # checker polls. Claiming it without preparing the host would park every
        # task on an allocation that cannot work.
        "labels": {"contour": "stand", "provisioning_phase": "complete"},
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
            # Capacity travels with the rest: without it a re-run silently
            # reverted a value corrected by hand, which is how the admission
            # label was lost between two deploys.
            for key in (
                "host",
                "public_ip",
                "ssh_user",
                "ssh_key",
                "status",
                "labels",
                "capacity_cpu",
                "capacity_ram_mb",
                "capacity_disk_mb",
            )
        }
        server = _request(
            f"{args.api_url}/api/servers/{args.handle}", internal_key, "PATCH", updates
        )

    print(f"{server['handle']}: status={server['status']} ssh_user={server['ssh_user']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
