#!/usr/bin/env python3
"""Wait, boundedly, for the image releases of the revision being deployed.

The deploy may only run a revision whose release markers exist: the worker base image
release and the control-plane service image release. Both are written last by the
push-to-main CI run of that exact commit (`Publish Worker Base Images` and `Publish
Service Image Release` in .github/workflows/ci.yml). A deploy dispatched right after a
merge used to fail at the marker lookup while that run was still going. This waits for
the run instead, and refuses as soon as waiting cannot help.

Each chain is waited for in turn, against one shared deadline. The marker itself is not
looked up here: each chain's consumer, run with `RELEASE_VALIDATION_ONLY=true`, is the one
probe (`infra/scripts/pull-worker-images.sh`, `infra/scripts/pull-service-images.sh`), and
its exit codes are the contract. Its "no marker" code is the only answer that leads to
asking GitHub about the CI run; every other refusal is final, and a registry failure is
retried a bounded number of times and then reported with the probe's own code.

It runs on the GitHub runner with the system interpreter, so it is stdlib-only.

Required environment:
  GITHUB_REPOSITORY  owner/name, as the runner sets it
  GITHUB_API_URL     the API base, as the runner sets it
  GITHUB_TOKEN       a token with actions:read on this repository
  GHCR_TOKEN, GHCR_OWNER — passed through to the probes, which require them

Exit codes: 0 when every marker resolves; the probe's own code for any refusal other
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
CI_WORKFLOW_FILE = "ci.yml"

PROBE_RELEASED = 0

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
class Chain:
    """One release chain, as the wait sees it: its probe and that probe's exit codes."""

    name: str
    probe_script: Path
    # The environment variable the probe reads the revision from.
    tag_variable: str
    # The CI job whose run writes the marker, named when it succeeded without one.
    publish_job: str
    no_release: int
    registry_failures: frozenset[int]


WORKER = Chain(
    name="worker",
    probe_script=REPO_ROOT / "infra" / "scripts" / "pull-worker-images.sh",
    tag_variable="WORKER_IMAGE_TAG",
    publish_job="Publish Worker Base Images",
    no_release=9,
    registry_failures=frozenset({10, 11, 12, 13}),
)
SERVICE = Chain(
    name="service",
    probe_script=REPO_ROOT / "infra" / "scripts" / "pull-service-images.sh",
    tag_variable="SERVICE_IMAGE_TAG",
    publish_job="Publish Service Image Release",
    no_release=9,
    registry_failures=frozenset({11}),
)
CHAINS = {chain.name: chain for chain in (WORKER, SERVICE)}


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


def run_probe(chain: Chain, sha: str, env: dict[str, str], script: Path | None = None) -> int:
    """Validate the chain's release marker without pulling any image; return its code."""
    with tempfile.TemporaryDirectory() as scratch:
        probe_env = dict(env, RELEASE_VALIDATION_ONLY="true")
        probe_env[chain.tag_variable] = sha
        # Required by the worker probe, never written in validation mode.
        probe_env["DIGEST_FILE"] = str(Path(scratch) / f"unused-{chain.name}-images.json")
        command = ["bash", str(script or chain.probe_script)]
        return subprocess.run(command, env=probe_env, check=False).returncode  # noqa: S603


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fail(code: int, message: str) -> int:
    _log(f"::error title=Release not deployable::{message}")
    return code


def wait_for_release(
    *,
    chain: Chain,
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
            _log(f"The {chain.name} release marker of {sha} exists.")
            return 0
        if status in chain.registry_failures:
            registry_failures += 1
            if registry_failures >= TRANSIENT_ATTEMPTS:
                return _fail(
                    status,
                    f"the registry failed the {chain.name} release lookup of {sha} "
                    f"{registry_failures} times in a row (probe exit {status}); see the probe "
                    "output above.",
                )
            _log(f"Registry lookup failed (probe exit {status}); retrying.")
        elif status != chain.no_release:
            return _fail(
                status,
                f"the {chain.name} release of {sha} was refused (probe exit {status}); "
                "waiting cannot fix it.",
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
                        f"{sha} has no {chain.name} release marker and no push-to-main run of "
                        f"{CI_WORKFLOW_FILE}, so nothing will ever publish it. "
                        "Only a commit merged to main can be deployed.",
                    )
                if run.status == "completed":
                    if run.conclusion != "success":
                        return _fail(
                            EXIT_CI_RUN_FAILED,
                            f"the CI run of {sha} finished '{run.conclusion}' without "
                            f"publishing a {chain.name} release: {run.url}",
                        )
                    if rechecked_after_success:
                        return _fail(
                            EXIT_RELEASED_WITHOUT_MARKER,
                            f"the CI run of {sha} succeeded but the revision has no "
                            f"{chain.name} release marker; check its {chain.publish_job} "
                            f"job: {run.url}",
                        )
                    rechecked_after_success = True
                    continue
                _log(
                    f"The CI run of {sha} is {run.status}; waiting for its {chain.name} "
                    f"release: {run.url}"
                )

        remaining = deadline - clock()
        if remaining <= 0:
            return _fail(
                EXIT_DEADLINE,
                f"the {chain.name} release of {sha} did not appear within "
                f"{timeout_seconds:.0f}s while its CI run was still going; dispatch the "
                "deploy again once it finishes.",
            )
        sleep(min(poll_seconds, remaining))


def wait_for_releases(
    *,
    chains: list[Chain],
    sha: str,
    timeout_seconds: float,
    poll_seconds: float,
    probe: Callable[[Chain], int],
    find_run: Callable[[], CiRun | None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait for every chain in turn; one deadline bounds them all."""
    deadline = clock() + timeout_seconds
    for chain in chains:
        status = wait_for_release(
            chain=chain,
            sha=sha,
            timeout_seconds=max(0.0, deadline - clock()),
            poll_seconds=poll_seconds,
            probe=lambda chain=chain: probe(chain),
            find_run=find_run,
            clock=clock,
            sleep=sleep,
        )
        if status != 0:
            return status
    return 0


def _required_env(names: tuple[str, ...]) -> dict[str, str] | None:
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        _log(f"FATAL: required environment is missing: {' '.join(missing)}")
        return None
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--revision", required=True, help="the full SHA being deployed")
    parser.add_argument(
        "--chain",
        action="append",
        choices=sorted(CHAINS),
        required=True,
        help="a release chain to wait for; repeat for several, waited for in order",
    )
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    env = _required_env(("GITHUB_REPOSITORY", "GITHUB_API_URL", "GITHUB_TOKEN"))
    if env is None:
        return EXIT_USAGE
    sha = args.revision
    return wait_for_releases(
        chains=[CHAINS[name] for name in args.chain],
        sha=sha,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        probe=lambda chain: run_probe(chain, sha, dict(os.environ)),
        find_run=lambda: fetch_ci_run(
            api_url=env["GITHUB_API_URL"],
            repository=env["GITHUB_REPOSITORY"],
            token=env["GITHUB_TOKEN"],
            sha=sha,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
