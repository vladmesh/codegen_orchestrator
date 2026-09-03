#!/usr/bin/env python3
"""Register one run-owned BitLaunch target as pending provisioning.

The workflow is the sole producer of this registration. It passes public
machine identity and the multiline creation key in separate protected files;
the caller removes both with its unconditional cleanup trap after the API has
encrypted the key at rest.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request

# This module runs under the host's system interpreter, which has no
# third-party packages: every import here has to be stdlib-only, transitively.
# `shared.provisioning_policy` is, and it is where the administrative account
# lives, so the row this script writes and the `ServerCreate.ssh_user` default a
# production row carries read the same constant and cannot drift. Reaching for
# the model itself instead cost paid run 33739202480, which died importing
# pydantic before the suite began.
from shared.provisioning_policy import (
    ADMIN_SSH_USER,
    BITLAUNCH_ID_LENGTH,
    BITLAUNCH_PROVIDER,
    parse_bitlaunch_server_id,
)

TARGET_ROLE = "target"


def build_target_payload(
    *, target_id: str, target_ip: str, run_tag: str, ssh_private_key: str
) -> dict[str, object]:
    """Build the API row for one exact target created by this workflow run."""
    # One parser for this identity, not a second copy of the rule: the copy is
    # how run 33248356742 came to refuse a target the rest of the system would
    # have accepted.
    if parse_bitlaunch_server_id(target_id) is None:
        raise ValueError(
            f"TARGET_ID must be a {BITLAUNCH_ID_LENGTH}-character lowercase hex BitLaunch ID"
        )
    if not target_ip or not run_tag or not ssh_private_key:
        raise ValueError("target IP, run tag, and SSH private key are required")

    return {
        "handle": f"bitlaunch-{target_id}",
        "host": target_ip,
        "public_ip": target_ip,
        "ssh_user": ADMIN_SSH_USER,
        "ssh_key": ssh_private_key,
        "capacity_cpu": 4,
        "capacity_ram_mb": 8192,
        "capacity_disk_mb": 153600,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ssh-private-key-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        registration_input = json.loads(args.input.read_text())
        if not isinstance(registration_input, dict):
            raise ValueError("registration input must be an object")
        payload = build_target_payload(
            target_id=str(registration_input["target_id"]),
            target_ip=str(registration_input["target_ip"]),
            run_tag=str(registration_input["run_tag"]),
            ssh_private_key=args.ssh_private_key_file.read_text(),
        )
        internal_key = os.environ["INTERNAL_API_KEY"]
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"target registration refused: {exc}", file=sys.stderr)
        return 2

    server = _request(f"{args.api_url}/api/servers/", internal_key, payload)
    print(f"registered {server.get('handle', payload['handle'])} as pending_setup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
