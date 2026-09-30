"""The deploy waits for the releases of its revision, boundedly, and only when it can help.

`scripts/wait_release.py` re-probes a chain's release marker while the push-to-main
CI run that publishes it is still queued or running, and refuses at once when that
run is absent, failed, or succeeded without a marker. The probe, the GitHub lookup,
the clock and the sleep are injected, so nothing here waits for real. The single-chain
tests run the worker chain; the service chain and the shared deadline follow them.
"""

from __future__ import annotations

from collections.abc import Iterator
import http.server
import json
from pathlib import Path
import stat
import threading

import pytest

from scripts import wait_release as wait
from scripts.wait_release import CiRun

SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_URL = "https://github.com/owner/repo/actions/runs/1"
TIMEOUT = 2700.0
POLL = 30.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Script:
    """Answers from a list, repeating the last answer once the list runs out."""

    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.calls = 0

    def __call__(self):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


def _run(probe: Script, find_run: Script, clock: FakeClock) -> int:
    return wait.wait_for_release(
        chain=wait.WORKER,
        sha=SHA,
        timeout_seconds=TIMEOUT,
        poll_seconds=POLL,
        probe=probe,
        find_run=find_run,
        clock=clock,
        sleep=clock.sleep,
    )


def _ci(status: str, conclusion: str | None = None) -> CiRun:
    return CiRun(url=RUN_URL, status=status, conclusion=conclusion)


def test_an_existing_marker_passes_without_asking_github():
    clock = FakeClock()
    find_run = Script([AssertionError("the GitHub API must not be called")])

    assert _run(Script([0]), find_run, clock) == 0
    assert find_run.calls == 0
    assert clock.sleeps == []


def test_waits_while_the_ci_run_is_going_and_passes_once_the_marker_appears():
    clock = FakeClock()
    probe = Script([9, 9, 9, 0])
    find_run = Script([_ci("queued"), _ci("in_progress"), _ci("in_progress")])

    assert _run(probe, find_run, clock) == 0
    assert probe.calls == 4
    assert clock.sleeps == [POLL, POLL, POLL]


def test_a_run_that_finishes_between_probe_and_lookup_is_rechecked_not_refused():
    clock = FakeClock()
    probe = Script([9, 0])
    find_run = Script([_ci("completed", "success")])

    assert _run(probe, find_run, clock) == 0
    assert clock.sleeps == []


def test_no_ci_run_for_the_sha_fails_at_once(capsys):
    clock = FakeClock()

    assert _run(Script([9]), Script([None]), clock) == wait.EXIT_NO_CI_RUN
    assert clock.sleeps == []
    assert "no push-to-main run of ci.yml" in capsys.readouterr().err


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
def test_a_ci_run_that_did_not_succeed_fails_at_once(capsys, conclusion):
    clock = FakeClock()

    result = _run(Script([9]), Script([_ci("completed", conclusion)]), clock)

    assert result == wait.EXIT_CI_RUN_FAILED
    assert clock.sleeps == []
    err = capsys.readouterr().err
    assert f"finished '{conclusion}'" in err
    assert RUN_URL in err


def test_a_successful_ci_run_without_a_marker_fails_after_one_recheck(capsys):
    clock = FakeClock()
    probe = Script([9])

    result = _run(probe, Script([_ci("completed", "success")]), clock)

    assert result == wait.EXIT_RELEASED_WITHOUT_MARKER
    assert probe.calls == 2
    assert clock.sleeps == []
    assert "succeeded but the revision has no worker release marker" in capsys.readouterr().err


def test_the_wait_ends_at_the_deadline(capsys):
    clock = FakeClock()
    probe = Script([9])
    find_run = Script([_ci("in_progress")])

    result = _run(probe, find_run, clock)

    assert result == wait.EXIT_DEADLINE
    assert sum(clock.sleeps) == TIMEOUT, "never sleeps past the deadline"
    assert max(clock.sleeps) <= POLL
    # One probe at the start and one after every sleep, including the last.
    assert probe.calls == len(clock.sleeps) + 1
    assert "did not appear within 2700s" in capsys.readouterr().err


def test_a_deadline_that_is_not_a_multiple_of_the_poll_is_honoured():
    clock = FakeClock()

    result = wait.wait_for_release(
        chain=wait.WORKER,
        sha=SHA,
        timeout_seconds=70,
        poll_seconds=POLL,
        probe=Script([9]),
        find_run=Script([_ci("queued")]),
        clock=clock,
        sleep=clock.sleep,
    )

    assert result == wait.EXIT_DEADLINE
    assert clock.sleeps == [30, 30, 10]


@pytest.mark.parametrize("code", [10, 11, 12, 13])
def test_a_persistent_registry_failure_is_a_failure_not_a_wait(code):
    clock = FakeClock()
    find_run = Script([AssertionError("a registry failure says nothing about CI")])
    probe = Script([code])

    assert _run(probe, find_run, clock) == code
    assert probe.calls == wait.TRANSIENT_ATTEMPTS
    assert len(clock.sleeps) == wait.TRANSIENT_ATTEMPTS - 1


def test_a_transient_registry_failure_is_retried():
    clock = FakeClock()

    assert _run(Script([11, 0]), Script([None]), clock) == 0
    assert clock.sleeps == [POLL]


@pytest.mark.parametrize("code", [1, 3, 6])
def test_any_other_refusal_from_the_probe_is_final(code):
    clock = FakeClock()
    probe = Script([code])

    assert _run(probe, Script([None]), clock) == code
    assert probe.calls == 1
    assert clock.sleeps == []


def test_a_persistent_github_api_failure_is_a_failure():
    clock = FakeClock()
    find_run = Script([wait.GitHubApiError("listing CI runs failed: HTTP 502")])

    assert _run(Script([9]), find_run, clock) == wait.EXIT_GITHUB_API
    assert find_run.calls == wait.TRANSIENT_ATTEMPTS


def test_the_exit_codes_are_distinct_from_each_other_and_from_the_probe():
    ours = {
        wait.EXIT_NO_CI_RUN,
        wait.EXIT_CI_RUN_FAILED,
        wait.EXIT_RELEASED_WITHOUT_MARKER,
        wait.EXIT_DEADLINE,
        wait.EXIT_GITHUB_API,
    }
    probe = {1, 3, 4, 5, 6, wait.WORKER.no_release, *wait.WORKER.registry_failures}

    assert len(ours) == 5
    assert not ours & probe


# --- the two real boundaries: the probe script and the GitHub API ---


def test_the_probe_runs_in_validation_only_mode(tmp_path: Path):
    seen = tmp_path / "seen"
    fake_probe = tmp_path / "probe.sh"
    fake_probe.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$RELEASE_VALIDATION_ONLY $DIGEST_FILE $WORKER_IMAGE_TAG" > "{seen}"\n'
        "exit 9\n"
    )
    fake_probe.chmod(fake_probe.stat().st_mode | stat.S_IXUSR)

    code = wait.run_probe(wait.WORKER, SHA, {"PATH": "/usr/bin:/bin"}, script=fake_probe)

    assert code == 9
    mode, digest_file, tag = seen.read_text().split()
    assert mode == "true"
    assert digest_file.endswith("unused-worker-images.json")
    assert tag == SHA


def test_the_default_probe_is_the_release_consumer():
    assert wait.WORKER.probe_script.name == "pull-worker-images.sh"
    assert wait.WORKER.probe_script.is_file()


class _GitHub(http.server.BaseHTTPRequestHandler):
    status = 200
    body: object = {}
    requests: list[tuple[str, dict[str, str]]] = []

    def do_GET(self) -> None:  # noqa: N802 -- http.server API
        type(self).requests.append((self.path, dict(self.headers)))
        payload = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def github() -> Iterator[tuple[str, type[_GitHub]]]:
    handler = type("GitHub", (_GitHub,), {"requests": [], "status": 200, "body": {}})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()


def _fetch(api_url: str) -> CiRun | None:
    return wait.fetch_ci_run(api_url=api_url, repository="owner/repo", token="t0ken", sha=SHA)  # noqa: S106


def test_the_lookup_asks_for_push_to_main_runs_of_ci_for_this_sha(github):
    api_url, handler = github
    handler.body = {
        "workflow_runs": [
            {
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-09-23T10:00:00Z",
                "html_url": "old",
            },
            {
                "head_sha": SHA,
                "status": "in_progress",
                "conclusion": None,
                "created_at": "2026-09-23T11:00:00Z",
                "html_url": "new",
            },
            {
                "head_sha": "other",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-23T12:00:00Z",
                "html_url": "other",
            },
        ]
    }

    run = _fetch(api_url)

    assert run == CiRun(url="new", status="in_progress", conclusion=None)
    path, headers = handler.requests[0]
    assert path.startswith("/repos/owner/repo/actions/workflows/ci.yml/runs?")
    assert f"head_sha={SHA}" in path
    assert "event=push" in path
    assert "branch=main" in path
    assert headers["Authorization"] == "Bearer t0ken"


def test_no_matching_run_is_none(github):
    api_url, handler = github
    handler.body = {"workflow_runs": []}

    assert _fetch(api_url) is None


@pytest.mark.parametrize(("status", "body"), [(502, {}), (200, {"message": "odd"})])
def test_an_unusable_answer_is_an_api_error(github, status, body):
    api_url, handler = github
    handler.status = status
    handler.body = body

    with pytest.raises(wait.GitHubApiError):
        _fetch(api_url)


# --- the service chain, and several chains under one deadline ---


def _run_chains(probes: dict[str, Script], find_run: Script, clock: FakeClock) -> int:
    return wait.wait_for_releases(
        chains=[wait.WORKER, wait.SERVICE],
        sha=SHA,
        timeout_seconds=TIMEOUT,
        poll_seconds=POLL,
        probe=lambda chain: probes[chain.name](),
        find_run=find_run,
        clock=clock,
        sleep=clock.sleep,
    )


def test_the_service_probe_is_the_service_release_consumer():
    assert wait.SERVICE.probe_script.name == "pull-service-images.sh"
    assert wait.SERVICE.probe_script.is_file()
    assert wait.SERVICE.tag_variable == "SERVICE_IMAGE_TAG"


def test_the_service_probe_runs_in_validation_only_mode_for_the_revision(tmp_path: Path):
    seen = tmp_path / "seen"
    fake_probe = tmp_path / "probe.sh"
    fake_probe.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$RELEASE_VALIDATION_ONLY $SERVICE_IMAGE_TAG" > "{seen}"\n'
        "exit 9\n"
    )

    assert wait.run_probe(wait.SERVICE, SHA, {"PATH": "/usr/bin:/bin"}, script=fake_probe) == 9
    assert seen.read_text().split() == ["true", SHA]


def test_both_chains_released_passes_without_asking_github():
    clock = FakeClock()
    probes = {"worker": Script([0]), "service": Script([0])}
    find_run = Script([AssertionError("the GitHub API must not be called")])

    assert _run_chains(probes, find_run, clock) == 0
    assert probes["worker"].calls == probes["service"].calls == 1


def test_a_revision_without_a_service_release_is_refused_after_a_released_worker_chain():
    clock = FakeClock()
    probes = {"worker": Script([0]), "service": Script([9])}

    result = _run_chains(probes, Script([_ci("completed", "success")]), clock)

    assert result == wait.EXIT_RELEASED_WITHOUT_MARKER
    assert probes["service"].calls == 2  # one re-probe after the successful run


def test_the_service_chain_waits_for_its_own_marker_while_the_run_is_going():
    clock = FakeClock()
    probes = {"worker": Script([0]), "service": Script([9, 9, 0])}

    assert _run_chains(probes, Script([_ci("in_progress")]), clock) == 0
    assert clock.sleeps == [POLL, POLL]


def test_one_deadline_bounds_every_chain():
    """The worker wait spends the budget; the service wait gets only what is left."""
    clock = FakeClock()
    probes = {"worker": Script([9] * 89 + [0]), "service": Script([9])}

    result = _run_chains(probes, Script([_ci("in_progress")]), clock)

    assert result == wait.EXIT_DEADLINE
    assert sum(clock.sleeps) == TIMEOUT


def test_a_service_registry_failure_is_retried_but_a_broken_release_is_final():
    clock = FakeClock()
    retried = {"worker": Script([0]), "service": Script([11, 0])}
    assert _run_chains(retried, Script([None]), clock) == 0

    clock = FakeClock()
    broken = Script([10])
    assert _run_chains({"worker": Script([0]), "service": broken}, Script([None]), clock) == 10
    assert broken.calls == 1


@pytest.mark.parametrize("chain", [wait.WORKER, wait.SERVICE])
def test_each_chain_names_its_publish_job_when_the_run_left_no_marker(chain, capsys):
    clock = FakeClock()
    result = wait.wait_for_release(
        chain=chain,
        sha=SHA,
        timeout_seconds=TIMEOUT,
        poll_seconds=POLL,
        probe=Script([chain.no_release]),
        find_run=Script([_ci("completed", "success")]),
        clock=clock,
        sleep=clock.sleep,
    )

    assert result == wait.EXIT_RELEASED_WITHOUT_MARKER
    assert chain.publish_job in capsys.readouterr().err
