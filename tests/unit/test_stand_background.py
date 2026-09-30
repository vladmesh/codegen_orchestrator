"""The stand's background bring-up work is joined bounded and fail-closed.

`scripts/stand_background.sh` is how stand-e2e.yml starts the release pulls and the uv
warm-up on the stand host right after bootstrap and joins each before its consumer. These
tests run it for real, with real child processes: a join has to hand back the job's own
failure, stop a job that hangs, and notice one that vanished, because a background job
that is silently passed over is a stand that runs on images nobody verified.
"""

import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stand_background.sh"

EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_NO_RECORD = 125


def _run(*args: str, env: dict[str, str] | None = None, timeout: float = 30):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "STAND_BACKGROUND_POLL_SECONDS": "0.05", **(env or {})},
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for(path: Path, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.02)


def test_start_returns_at_once_and_the_job_outlives_the_starting_shell(tmp_path):
    started = time.monotonic()
    result = _run("start", str(tmp_path), "slow", "bash", "-c", "sleep 1; echo done")

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 1, "start must not wait for the job"
    assert (tmp_path / "slow.started").read_text().strip().isdigit()
    assert not (tmp_path / "slow.status").exists()

    joined = _run("join", str(tmp_path), "slow", "10")

    assert joined.returncode == 0, joined.stderr
    assert "done" in joined.stdout
    assert "background job slow finished in" in joined.stdout
    assert (tmp_path / "slow.status").read_text().strip() == "0"


def test_the_job_inherits_the_exported_environment_not_the_command_line(tmp_path):
    result = _run(
        "start",
        str(tmp_path),
        "env",
        "bash",
        "-c",
        'test "${PASSED_ON_STDIN_TOKEN}" = sekret && echo inherited',
        env={"PASSED_ON_STDIN_TOKEN": "sekret"},
    )
    assert result.returncode == 0, result.stderr

    joined = _run("join", str(tmp_path), "env", "10")

    assert joined.returncode == 0, joined.stderr
    assert "inherited" in joined.stdout


def test_a_failed_job_fails_the_join_with_its_own_code_and_its_log_tail(tmp_path):
    script = "for i in $(seq 1 100); do echo line-$i; done; echo the-reason >&2; exit 9"
    _run("start", str(tmp_path), "pull", "bash", "-c", script)

    joined = _run("join", str(tmp_path), "pull", "10")

    assert joined.returncode == 9
    assert "background job pull failed with exit 9" in joined.stderr
    assert "the-reason" in joined.stderr
    assert "line-100" in joined.stderr
    assert "line-1\n" not in joined.stderr, "the tail is bounded, the full log is grouped"
    assert "line-1\n" in joined.stdout


def test_a_job_that_hangs_is_stopped_and_fails_the_join(tmp_path):
    _run("start", str(tmp_path), "hang", "bash", "-c", "echo waiting; sleep 300")
    _wait_for(tmp_path / "hang.pid")
    pid = int((tmp_path / "hang.pid").read_text())

    joined = _run("join", str(tmp_path), "hang", "1")

    assert joined.returncode == EXIT_TIMEOUT
    assert "did not finish within 1s" in joined.stderr
    assert "waiting" in joined.stderr
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "a timed-out job is stopped, not left running"


def test_a_job_that_vanished_without_a_status_fails_the_join(tmp_path):
    _run("start", str(tmp_path), "gone", "bash", "-c", "echo partial; sleep 300")
    _wait_for(tmp_path / "gone.pid")
    pid = int((tmp_path / "gone.pid").read_text())
    os.killpg(pid, signal.SIGKILL)

    joined = _run("join", str(tmp_path), "gone", "30")

    assert joined.returncode == EXIT_NO_RECORD
    assert "recorded no exit status" in joined.stderr
    assert "partial" in joined.stderr


def test_a_job_that_was_never_started_fails_the_join(tmp_path):
    joined = _run("join", str(tmp_path), "missing", "5")

    assert joined.returncode == EXIT_NO_RECORD
    assert "was never started" in joined.stderr


def test_a_corrupt_status_is_not_read_as_success(tmp_path):
    for name, value in (("started", "100"), ("finished", "110"), ("status", "ok")):
        (tmp_path / f"odd.{name}").write_text(value + "\n")

    joined = _run("join", str(tmp_path), "odd", "5")

    assert joined.returncode == EXIT_NO_RECORD


def test_a_job_name_is_started_once(tmp_path):
    assert _run("start", str(tmp_path), "once", "true").returncode == 0

    again = _run("start", str(tmp_path), "once", "true")

    assert again.returncode == EXIT_USAGE
    assert "already started" in again.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("start",),
        ("start", "DIR", "job"),
        ("start", "DIR", "../escape", "true"),
        ("join", "DIR", "job"),
        ("join", "DIR", "job", "0"),
        ("join", "DIR", "job", "ten"),
        ("report", "DIR"),
        ("unknown", "DIR", "job"),
    ],
)
def test_usage_errors_are_refused(tmp_path, args):
    result = _run(*(str(tmp_path) if arg == "DIR" else arg for arg in args))

    assert result.returncode == EXIT_USAGE
    assert "Usage:" in result.stderr


def test_report_states_every_job_and_never_judges(tmp_path):
    _run("start", str(tmp_path), "fine", "true")
    _run("start", str(tmp_path), "broken", "bash", "-c", "exit 3")
    _run("start", str(tmp_path), "running", "sleep", "300")
    for job in ("fine", "broken"):
        _wait_for(tmp_path / f"{job}.status")

    result = _run("report", str(tmp_path), "fine", "broken", "running", "absent")

    assert result.returncode == 0, result.stderr
    rows = result.stdout.splitlines()
    assert rows[0] == "| background job | status | seconds |"
    assert any(row.startswith("| fine | exit 0 | ") for row in rows)
    assert any(row.startswith("| broken | exit 3 | ") for row in rows)
    assert any(row.startswith("| running | unfinished | ") for row in rows)
    assert "| absent | not started | - |" in rows
    _wait_for(tmp_path / "running.pid")
    os.killpg(int((tmp_path / "running.pid").read_text()), signal.SIGKILL)
