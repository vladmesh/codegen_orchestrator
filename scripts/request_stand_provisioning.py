#!/usr/bin/env python3
"""Ask the API to enqueue provisioning for an already registered stand target."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--handle", required=True)
    parser.add_argument("--profile", choices=("stand_e2e",))
    args = parser.parse_args()

    internal_key = os.environ.get("INTERNAL_API_KEY")
    if not internal_key:
        print("INTERNAL_API_KEY is required", file=sys.stderr)
        return 2

    payload = {"profile": args.profile} if args.profile else None
    request = urllib.request.Request(  # noqa: S310 -- workflow-owned local API URL
        f"{args.api_url}/api/servers/{args.handle}/provision",
        method="POST",
        data=json.dumps(payload).encode() if payload else None,
        headers={
            "X-Internal-Key": internal_key,
            **({"Content-Type": "application/json"} if payload else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or not isinstance(payload.get("request_id"), str):
        print("provisioning request response is malformed", file=sys.stderr)
        return 1
    print(f"queued provisioning for {args.handle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
