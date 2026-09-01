#!/usr/bin/env python3
"""Wait for the provisioner-owned completion state of a dynamic stand target."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import sys
import time
import urllib.request

from shared.constants import Timeouts

# The allocator's freshness window is 300s; never sit closer than this to its
# edge when handing the target to a suite.
MIN_METRICS_MARGIN_SECONDS = 60
HEALTH_CHECK_OBSERVER_RESERVE_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = (
    Timeouts.ACCESS_PHASE + Timeouts.PROVISIONING + HEALTH_CHECK_OBSERVER_RESERVE_SECONDS
)
TERMINAL_FAILURE_STATUSES = frozenset({"error", "unreachable", "missing", "decommissioned"})


def _read_server(api_url: str, handle: str, internal_key: str) -> dict:
    request = urllib.request.Request(  # noqa: S310 -- workflow-owned local API URL
        f"{api_url}/api/servers/{handle}",
        headers={"X-Internal-Key": internal_key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read())
    return payload if isinstance(payload, dict) else {}


def provisioning_snapshot(
    server: dict,
    *,
    observed_at: str | None = None,
) -> dict[str, object]:
    """Return the allow-listed state that may cross the stand boundary."""
    labels = server.get("labels")
    phase = labels.get("provisioning_phase") if isinstance(labels, dict) else None
    return {
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "handle": server.get("handle"),
        "status": server.get("status"),
        "provisioning_phase": phase,
        "provisioning_attempts": server.get("provisioning_attempts"),
        "provisioning_started_at": server.get("provisioning_started_at"),
        "last_health_check": server.get("last_health_check"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handle", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--metrics-freshness-seconds",
        type=int,
        default=300,
        help="must match the allocator's allocation_metrics_freshness_seconds",
    )
    parser.add_argument(
        "--require-fresh-metrics",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "wait for allocator-ready telemetry (disable only before an immediate canonical probe)"
        ),
    )
    args = parser.parse_args()
    internal_key = os.environ.get("INTERNAL_API_KEY")
    if not internal_key:
        print("INTERNAL_API_KEY is required", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        server = _read_server(args.api_url, args.handle, internal_key)
        print(json.dumps(provisioning_snapshot(server), sort_keys=True), flush=True)
        labels = server.get("labels") if isinstance(server, dict) else None
        if (
            isinstance(server, dict)
            and server.get("status") == "ready"
            and isinstance(labels, dict)
            and labels.get("provisioning_phase") == "complete"
            and (
                not args.require_fresh_metrics
                or _metrics_are_fresh(
                    server.get("last_health_check"), args.metrics_freshness_seconds
                )
            )
        ):
            print(f"provisioning complete for {args.handle}")
            return 0
        if server.get("status") in TERMINAL_FAILURE_STATUSES:
            print(
                f"provisioning reached terminal failure for {args.handle}",
                file=sys.stderr,
            )
            return 1
        time.sleep(5)
    print(f"provisioning did not complete for {args.handle}", file=sys.stderr)
    return 1


def _metrics_are_fresh(stamp: str | None, freshness_seconds: int) -> bool:
    """Whether the allocator would accept this host's telemetry right now.

    A freshly provisioned host is `ready` with a complete provisioning phase
    several minutes before the health checker first stamps it, and the allocator
    refuses a host whose telemetry is older than its freshness window with
    `no_fresh_metrics`. Waiting only for the provisioner therefore hands the
    suite a target that cannot be allocated to, which reads as a product failure
    (`Engineering failed, task status: waiting_human_review`) rather than as the
    race it is. "Ready" has to mean "allocatable", so this waits for the first
    stamp too.

    The margin keeps the suite from starting on telemetry that is about to go
    stale during allocation.
    """
    if not stamp:
        return False
    try:
        checked_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    margin = min(MIN_METRICS_MARGIN_SECONDS, freshness_seconds // 2)
    age = (datetime.now(UTC) - checked_at).total_seconds()
    return 0 <= age < freshness_seconds - margin


if __name__ == "__main__":
    raise SystemExit(main())
