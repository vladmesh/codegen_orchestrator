#!/usr/bin/env python3
"""Reject direct application write requests before Claude executes Bash."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

WRITE_METHODS = r"POST|PUT|PATCH|DELETE"


def _curl_write(command: str, target: str) -> str | None:
    """Detect a curl write even when its URL and method flag are reordered."""
    escaped_target = re.escape(target.rstrip("/"))
    url = re.search(rf"{escaped_target}[^\s'\"]*", command, flags=re.IGNORECASE)
    if not url:
        return None

    method = re.search(
        rf"(?:-X|--request)\s*({WRITE_METHODS})\b",
        command,
        flags=re.IGNORECASE,
    )
    if method:
        return f"{method.group(1).upper()} {url.group(0)}"

    # curl sends POST for a request body unless --get explicitly changes that
    # behavior. This is still a direct application write attempt.
    has_body = re.search(
        r"(?:-d|--data(?:-raw|-binary|-ascii)?)(?:=|\s)", command, flags=re.IGNORECASE
    )
    is_get = re.search(r"(?:-G|--get)\b", command, flags=re.IGNORECASE)
    if has_body and not is_get:
        return f"POST {url.group(0)}"
    return None


def forbidden_write(command: str, target: str) -> str | None:
    escaped_target = re.escape(target.rstrip("/"))
    patterns = (
        rf"(?i)\b({WRITE_METHODS})\s+({escaped_target}[^\s'\"]*)",
        rf"(?i)(?:-X|--request)\s*({WRITE_METHODS})\b[^\n]*?({escaped_target}[^\s'\"]*)",
        rf"(?i)\b(?:requests|httpx)\.({_method_pattern()})\s*\(\s*['\"]({escaped_target}[^'\"]*)",
        rf"(?i)\b(?:requests|httpx)\.request\s*\(\s*['\"]({WRITE_METHODS})['\"]\s*,\s*['\"]({escaped_target}[^'\"]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            if len(match.groups()) == 1:
                return f"POST {match.group(1)}"
            return f"{match.group(1).upper()} {match.group(2)}"
    return _curl_write(command, target) if re.search(r"\bcurl\b", command) else None


def _method_pattern() -> str:
    return WRITE_METHODS.lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    write = forbidden_write(command, args.target)
    if not write:
        return 0
    Path(args.trace).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.trace).open("a") as trace:
        trace.write(write + "\n")
    print(f"Direct application write is forbidden: {write}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
