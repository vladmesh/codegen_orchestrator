#!/usr/bin/env python3
"""Register one run-owned BitLaunch target as pending provisioning.

The workflow is the sole producer of this registration. It passes the public
machine identity through its environment and keeps the creation SSH key in the
process environment until the API encrypts it at rest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BITLAUNCH_PROVIDER = "bitlaunch"
TARGET_ROLE = "target"


def build_target_payload(
    *, target_id: str, target_ip: str, run_tag: str, ssh_private_key: str
) -> dict[str, object]:
    """Build the API row for one exact target created by this workflow run."""
    if not target_id.isascii() or not target_id.isdecimal() or int(target_id) <= 0:
        raise ValueError("TARGET_ID must be a positive decimal BitLaunch ID")
    if not target_ip or not run_tag or not ssh_private_key:
        raise ValueError("target IP, run tag, and SSH private key are required")

    return {
        "handle": f"bitlaunch-{target_id}",
        "host": target_ip,
        "public_ip": target_ip,
        "ssh_user": "root",
        "ssh_key": ssh_private_key,
        "capacity_cpu": 2,
        "capacity_ram_mb": 4096,
        "capacity_disk_mb": 40960,
        "is_managed": True,
        "status": "pending_setup",
        "notes": "Run-owned BitLaunch e2e target; provisioner-only completion.",
        "labels": {
            "contour": "stand",
            "provider": BITLAUNCH_PROVIDER,
            "provider_id": target_id,
            "stand_run_tag": run_tag,
            "stand_role": TARGET_ROLE,
        },
    }


def _request(url: str, key: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(  # noqa: S310 -- workflow-owned local API URL
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Internal-Key": key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        parsed = json.loads(response.read())
    if not isinstance(parsed, dict):
        raise RuntimeError("server registration response is malformed")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    try:
        payload = build_target_payload(
            target_id=os.environ["TARGET_ID"],
            target_ip=os.environ["TARGET_IP"],
            run_tag=os.environ["STAND_RUN_TAG"],
            ssh_private_key=os.environ["SSH_PRIVATE_KEY"],
        )
        internal_key = os.environ["INTERNAL_API_KEY"]
    except (KeyError, ValueError) as exc:
        print(f"target registration refused: {exc}", file=sys.stderr)
        return 2

    server = _request(f"{args.api_url}/api/servers/", internal_key, payload)
    print(f"registered {server.get('handle', payload['handle'])} as pending_setup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
