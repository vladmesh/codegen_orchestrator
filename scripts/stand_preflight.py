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
import urllib.error
import urllib.request

MIN_FREE_DISK_GB = 10
# The statuses shared/server_admission.py admits a project application on.
ADMITTING_STATUSES = frozenset({"active", "ready", "in_use"})
DEFAULT_API_URL = "http://127.0.0.1:8000"
# The stand deploys onto itself, so the same disk carries the orchestrator, the
# worker images and every stack a run brings up.


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


def check_claude_session(profile: str | None, probe: bool = True) -> tuple[str, bool, str]:
    """Apply worker-manager's local validator without using the provider."""
    try:
        sys.path.insert(0, str(Path(__file__).parents[1] / "services" / "worker-manager"))
        from src.claude_auth import validate_claude_host_session

        validate_claude_host_session(profile)
    except (ImportError, RuntimeError) as exc:
        return _fail("claude session", str(exc))

    return _ok("claude session", "local session material is valid")


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
    return _ok("codex session", "local session material is valid")


def check_stand_token_credentials() -> tuple[str, bool, str]:
    """Apply the pre-create token validator on the stand, never host profiles."""
    from shared.stand_credentials import CredentialShape, validate_stand_token_credentials

    failures = validate_stand_token_credentials(os.environ, shape=CredentialShape.STAND_HOST)
    if failures:
        return _fail(
            "stand token authentication",
            "; ".join(f"{item.name}: {item.detail}" for item in failures),
        )
    return _ok("stand token authentication", "local token prerequisites are valid")


def check_deploy_target(api_url: str, internal_key: str | None) -> tuple[str, bool, str]:
    """A live run needs a server the allocator will actually admit.

    Admission (shared/server_admission.py) wants a managed server whose status
    admits and whose software provisioning is recorded complete. Without one,
    every task parks on `waiting_resources` and the run dies at its own timeout
    with nothing to show — which is how two runs were spent before this check
    existed.
    """
    if not internal_key:
        return _fail("deploy target", "INTERNAL_API_KEY is unset, cannot ask the API")

    request = urllib.request.Request(  # noqa: S310 — operator-supplied http(s) URL
        f"{api_url}/api/servers/",
        headers={"X-Internal-Key": internal_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            servers = json.loads(response.read())
    except (OSError, json.JSONDecodeError) as exc:
        return _fail("deploy target", f"cannot read the server list: {exc}")

    admitting = [
        server
        for server in servers
        if server.get("is_managed")
        and server.get("status") in ADMITTING_STATUSES
        and (server.get("labels") or {}).get("provisioning_phase") == "complete"
    ]
    if not admitting:
        seen = ", ".join(
            f"{s.get('handle')}={s.get('status')}"
            f"/{(s.get('labels') or {}).get('provisioning_phase', '-')}"
            f"/managed={bool(s.get('is_managed'))}"
            for s in servers
        )
        return _fail("deploy target", f"no server the allocator would admit; saw: {seen or 'none'}")

    handles = ", ".join(str(server.get("handle")) for server in admitting)
    return _ok("deploy target", handles)


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
        check_deploy_target(
            os.environ.get("STAND_API_URL", DEFAULT_API_URL),
            os.environ.get("INTERNAL_API_KEY"),
        ),
    ]
    if os.environ.get("LIVE_CONTOUR") == "stand":
        results.append(check_stand_token_credentials())
    else:
        results.extend(
            [
                check_claude_session(os.environ.get("HOST_CLAUDE_DIR")),
                check_codex_session(os.environ.get("HOST_CODEX_HOME")),
            ]
        )

    for name, passed, detail in results:
        mark = "ok  " if passed else "FAIL"
        print(f"{mark} {name}{f': {detail}' if detail else ''}")

    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
