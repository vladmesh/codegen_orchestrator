#!/usr/bin/env python3
"""Wait for the provisioner-owned completion state of a dynamic stand target."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handle", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    internal_key = os.environ.get("INTERNAL_API_KEY")
    if not internal_key:
        print("INTERNAL_API_KEY is required", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        request = urllib.request.Request(  # noqa: S310 -- workflow-owned local API URL
            f"{args.api_url}/api/servers/{args.handle}",
            headers={"X-Internal-Key": internal_key},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            server = json.loads(response.read())
        labels = server.get("labels") if isinstance(server, dict) else None
        if (
            isinstance(server, dict)
            and server.get("status") == "ready"
            and isinstance(labels, dict)
            and labels.get("provisioning_phase") == "complete"
        ):
            print(f"provisioning complete for {args.handle}")
            return 0
        time.sleep(5)
    print(f"provisioning did not complete for {args.handle}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
