#!/usr/bin/env python3
"""Check the stand can run a live pipeline before one is started.

A mega run costs ten minutes and a worker's subscription quota. Every condition
below is one that would otherwise be discovered halfway through it: an expired
Claude session, a Codex profile the worker-manager refuses, a full disk. Failing
here costs seconds and says what to fix.

The subscriptions are the reason this exists. A stand idles between runs, and an
idle session is exactly the one that goes stale — its tokens refresh when they
are used, and nothing uses them.

    ./scripts/stand_preflight.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

MIN_FREE_DISK_GB = 10
# The stand deploys onto itself, so the same disk carries the orchestrator, the
# worker images and every stack a run brings up.
CLAUDE_EXPIRY_MARGIN_SECONDS = 24 * 3600


def _fail(check: str, detail: str) -> tuple[str, bool, str]:
    return (check, False, detail)


def _ok(check: str, detail: str = "") -> tuple[str, bool, str]:
    return (check, True, detail)


def check_contour() -> tuple[str, bool, str]:
    contour = os.environ.get("LIVE_CONTOUR")
    if contour != "stand":
        return _fail(
            "contour",
            f"LIVE_CONTOUR={contour!r}; a stand run must name its own contour or it "
            "would create — and sweep — production's names",
        )
    return _ok("contour", "stand")


def check_claude_session(profile: str | None) -> tuple[str, bool, str]:
    if not profile:
        return _fail("claude session", "HOST_CLAUDE_DIR is unset")
    credentials = Path(profile) / ".credentials.json"
    if not credentials.is_file():
        return _fail("claude session", f"no .credentials.json in {profile}; run claude auth login")
    try:
        data = json.loads(credentials.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _fail("claude session", f"unreadable credentials: {exc}")

    oauth = data.get("claudeAiOauth") or {}
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, int | float):
        # A session without a stated expiry is not proof of a broken one; the
        # live check below is what decides.
        return _ok("claude session", "present, no expiry stated")
    remaining = expires_at / 1000 - time.time()
    if remaining <= 0:
        return _fail("claude session", "expired; run claude auth login")
    if remaining < CLAUDE_EXPIRY_MARGIN_SECONDS:
        return _fail(
            "claude session", f"expires in {remaining / 3600:.1f}h; refresh before running"
        )
    return _ok("claude session", f"valid for {remaining / 3600:.0f}h")


def check_codex_session(profile: str | None) -> tuple[str, bool, str]:
    """Apply the worker-manager's own rules, not a second copy of them."""
    sys.path.insert(0, str(Path(__file__).parents[1] / "services" / "worker-manager"))
    try:
        from src.codex_auth import validate_codex_host_session
    except ImportError as exc:
        return _fail("codex session", f"cannot import the worker-manager check: {exc}")
    try:
        validate_codex_host_session(profile)
    except RuntimeError as exc:
        return _fail("codex session", str(exc))
    return _ok("codex session", profile or "")


def check_disk() -> tuple[str, bool, str]:
    free_gb = shutil.disk_usage("/").free / 1024**3
    if free_gb < MIN_FREE_DISK_GB:
        return _fail("disk", f"{free_gb:.1f} GB free, need {MIN_FREE_DISK_GB} GB")
    return _ok("disk", f"{free_gb:.1f} GB free")


def check_docker() -> tuple[str, bool, str]:
    try:
        subprocess.run(  # noqa: S603
            ["docker", "info"],  # noqa: S607
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail("docker", str(exc))
    return _ok("docker")


def main() -> int:
    results = [
        check_contour(),
        check_docker(),
        check_disk(),
        check_claude_session(os.environ.get("HOST_CLAUDE_DIR")),
        check_codex_session(os.environ.get("HOST_CODEX_HOME")),
    ]

    for name, passed, detail in results:
        mark = "ok  " if passed else "FAIL"
        print(f"{mark} {name}{f': {detail}' if detail else ''}")

    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
