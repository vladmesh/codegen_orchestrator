#!/usr/bin/env python3
"""Wait, boundedly, for the worker base image release of the revision being deployed.

The deploy may only run a revision whose worker release marker exists; the marker is
written last by the `Publish Worker Base Images` job of the push-to-main CI run of
that exact commit (.github/workflows/ci.yml). A deploy dispatched right after a merge
used to fail at the marker lookup while that run was still going. This waits for the
run instead, and refuses as soon as waiting cannot help.

The marker itself is not looked up here: `infra/scripts/pull-worker-images.sh` in
`RELEASE_VALIDATION_ONLY=true` mode is the one probe, and its exit codes are the
contract. Exit 9 (no marker) is the only answer that leads to asking GitHub about the
CI run; every other refusal is final, and a registry failure (10-13) is retried a
bounded number of times and then reported with the probe's own code.

It runs on the GitHub runner with the system interpreter, so it is stdlib-only.

Required environment:
  WORKER_IMAGE_TAG   the deployed commit SHA (the probe reads it too)
  GITHUB_REPOSITORY  owner/name, as the runner sets it
  GITHUB_API_URL     the API base, as the runner sets it
  GITHUB_TOKEN       a token with actions:read on this repository
  GHCR_TOKEN, GHCR_OWNER — passed through to the probe, which requires them

Exit codes: 0 when the marker resolves; the probe's own code for any refusal other
than "no marker"; and one code per reason this script gives up:
  20  there is no push-to-main CI run of ci.yml for this SHA
  21  that CI run completed without success
  22  that CI run succeeded, yet the SHA has no release marker
  23  the deadline passed while the CI run was still queued or running
  24  the GitHub API could not answer which CI run this SHA has
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_SCRIPT = REPO_ROOT / "infra" / "scripts" / "pull-worker-images.sh"
CI_WORKFLOW_FILE = "ci.yml"

PROBE_RELEASED = 0
PROBE_NO_RELEASE = 9
PROBE_REGISTRY_FAILURES = frozenset({10, 11, 12, 13})

EXIT_NO_CI_RUN = 20
EXIT_CI_RUN_FAILED = 21
EXIT_RELEASED_WITHOUT_MARKER = 22
EXIT_DEADLINE = 23
EXIT_GITHUB_API = 24
EXIT_USAGE = 2

# Consecutive transient failures (registry or GitHub API) tolerated before giving up.
TRANSIENT_ATTEMPTS = 3
DEFAULT_POLL_SECONDS = 30


@dataclass(frozen=True)
class CiRun:
    url: str
    status: str
    conclusion: str | None


class GitHubApiError(RuntimeError):
    """The GitHub API gave no usable answer about the CI runs of a SHA."""


def fetch_ci_run(*, api_url: str, repository: str, token: str, sha: str) -> CiRun | None:
    """Return the newest push-to-main run of ci.yml for `sha`, or None if there is none."""
    query = urllib.parse.urlencode(
        {"head_sha": sha, "event": "push", "branch": "main", "per_page": "20"}
    )
    request = urllib.request.Request(  # noqa: S310 -- the runner's own GitHub API URL
        f"{api_url}/repos/{repository}/actions/workflows/{CI_WORKFLOW_FILE}/runs?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise GitHubApiError(f"listing CI runs for {sha} failed: {error}") from error
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise GitHubApiError(f"listing CI runs for {sha} returned no workflow_runs list")
    matching = [run for run in runs if isinstance(run, dict) and run.get("head_sha") == sha]
    if not matching:
        return None
    newest = max(matching, key=lambda run: str(run.get("created_at", "")))
    return CiRun(
        url=str(newest.get("html_url", "")),
        status=str(newest.get("status", "")),
        conclusion=newest.get("conclusion"),
    )


def run_probe(env: dict[str, str], script: Path = PROBE_SCRIPT) -> int:
    """Validate the release marker without pulling any image; return the probe's code."""
    with tempfile.TemporaryDirectory() as scratch:
        probe_env = dict(env, RELEASE_VALIDATION_ONLY="true")
        # Required by the probe, never written in validation mode.
        probe_env["DIGEST_FILE"] = str(Path(scratch) / "unused-worker-images.json")
        command = ["bash", str(script)]
        return subprocess.run(command, env=probe_env, check=False).returncode  # noqa: S603


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fail(code: int, message: str) -> int:
    _log(f"::error title=Worker release not deployable::{message}")
    return code


def wait_for_release(
    *,
    sha: str,
    timeout_seconds: float,
    poll_seconds: float,
    probe: Callable[[], int],
    find_run: Callable[[], CiRun | None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    deadline = clock() + timeout_seconds
    registry_failures = 0
    api_failures = 0
    # A run can finish between a probe and the run lookup; one re-probe tells a marker
    # that just landed from a successful run that never wrote one.
    rechecked_after_success = False

    while True:
        status = probe()
        if status == PROBE_RELEASED:
            _log(f"The worker release marker of {sha} exists.")
            return 0
        if status in PROBE_REGISTRY_FAILURES:
            registry_failures += 1
            if registry_failures >= TRANSIENT_ATTEMPTS:
                return _fail(
                    status,
                    f"the registry failed the release lookup of {sha} {registry_failures} times "
                    f"in a row (probe exit {status}); see the probe output above.",
                )
            _log(f"Registry lookup failed (probe exit {status}); retrying.")
        elif status != PROBE_NO_RELEASE:
            return _fail(
                status,
                f"the release of {sha} was refused (probe exit {status}); waiting cannot fix it.",
            )
        else:
            registry_failures = 0
            try:
                run = find_run()
            except GitHubApiError as error:
                api_failures += 1
                if api_failures >= TRANSIENT_ATTEMPTS:
                    return _fail(EXIT_GITHUB_API, f"{error} ({api_failures} times in a row).")
                _log(f"{error}; retrying.")
            else:
                api_failures = 0
                if run is None:
                    return _fail(
                        EXIT_NO_CI_RUN,
                        f"{sha} has no release marker and no push-to-main run of "
                        f"{CI_WORKFLOW_FILE}, so nothing will ever publish it. "
                        "Only a commit merged to main can be deployed.",
                    )
                if run.status == "completed":
                    if run.conclusion != "success":
                        return _fail(
                            EXIT_CI_RUN_FAILED,
                            f"the CI run of {sha} finished '{run.conclusion}' without "
                            f"publishing a worker release: {run.url}",
                        )
                    if rechecked_after_success:
                        return _fail(
                            EXIT_RELEASED_WITHOUT_MARKER,
                            f"the CI run of {sha} succeeded but the revision has no worker "
                            f"release marker; check its Publish Worker Base Images job: {run.url}",
                        )
                    rechecked_after_success = True
                    continue
                _log(f"The CI run of {sha} is {run.status}; waiting for its release: {run.url}")

        remaining = deadline - clock()
        if remaining <= 0:
            return _fail(
                EXIT_DEADLINE,
                f"the worker release of {sha} did not appear within {timeout_seconds:.0f}s "
                "while its CI run was still going; dispatch the deploy again once it finishes.",
            )
        sleep(min(poll_seconds, remaining))


def _required_env(names: tuple[str, ...]) -> dict[str, str] | None:
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        _log(f"FATAL: required environment is missing: {' '.join(missing)}")
        return None
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    env = _required_env(("WORKER_IMAGE_TAG", "GITHUB_REPOSITORY", "GITHUB_API_URL", "GITHUB_TOKEN"))
    if env is None:
        return EXIT_USAGE
    sha = env["WORKER_IMAGE_TAG"]
    return wait_for_release(
        sha=sha,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        probe=lambda: run_probe(dict(os.environ)),
        find_run=lambda: fetch_ci_run(
            api_url=env["GITHUB_API_URL"],
            repository=env["GITHUB_REPOSITORY"],
            token=env["GITHUB_TOKEN"],
            sha=sha,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
