"""The deploy's file-only SSH steps survive a dropped connection, and only that.

`infra/scripts/deploy-ssh.sh` runs a remote script over SSH and retries when ssh
itself fails (exit 255) — a bounded number of times. A script that ran and failed is
a real refusal and must never run a second time. These tests run the helper for real
against a fake `ssh` on PATH that either fails to connect or runs the remote command
in a local shell, and a fake `sleep` that records the backoff instead of waiting.
"""

from pathlib import Path
import stat
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "infra" / "scripts" / "deploy-ssh.sh"
MAX_ATTEMPTS = 3
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----"
)

# A fake ssh. Attempt N follows the N-th entry of FAKE_SSH_PLAN: `drop` exits 255
# without running anything, the way ssh reports a connection it could not make;
# `run` runs the remote command in a local shell with the script on stdin, the way
# sshd hands it to the login shell. Every attempt records its argv, the stdin it was
# given and the key file it was pointed at.
FAKE_SSH = """#!/usr/bin/env bash
set -uo pipefail
attempt=$(( $(cat "${FAKE_SSH_DIR}/attempts" 2>/dev/null || echo 0) + 1 ))
echo "${attempt}" > "${FAKE_SSH_DIR}/attempts"
printf '%s\\n' "$@" > "${FAKE_SSH_DIR}/argv.${attempt}"
key=""
previous=""
for arg in "$@"; do
    if [ "${previous}" = "-i" ]; then key="${arg}"; fi
    previous="${arg}"
done
cp "${key}" "${FAKE_SSH_DIR}/key.${attempt}"
stat -c '%a' "${key}" > "${FAKE_SSH_DIR}/key-mode.${attempt}"
echo "${key}" > "${FAKE_SSH_DIR}/key-path"
remote="${@: -1}"
IFS=',' read -r -a plan <<< "${FAKE_SSH_PLAN}"
step="${plan[$((attempt - 1))]:-run}"
if [ "${step}" = "drop" ]; then
    cat > "${FAKE_SSH_DIR}/stdin.${attempt}"
    echo "partial output of a dropped attempt"
    echo "ssh: connect to host deploy.example port 22: Connection timed out" >&2
    exit 255
fi
tee "${FAKE_SSH_DIR}/stdin.${attempt}" | sh -c "${remote}"
"""

FAKE_SLEEP = """#!/usr/bin/env bash
echo "$1" >> "${FAKE_SSH_DIR}/sleeps"
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_host(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "ssh", FAKE_SSH)
    _write_executable(bin_dir / "sleep", FAKE_SLEEP)
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path


def _run(fake_host: Path, script: str, plan: str) -> subprocess.CompletedProcess[str]:
    state = fake_host / "state"
    return subprocess.run(
        ["bash", str(HELPER)],
        input=script,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{fake_host / 'bin'}:/usr/bin:/bin",
            "FAKE_SSH_DIR": str(state),
            "FAKE_SSH_PLAN": plan,
            "SSH_PRIVATE_KEY": PRIVATE_KEY,
            "PROD_HOST": "deploy.example",
            "DEPLOY_SSH_USER": "deploy",
        },
    )


def _attempts(fake_host: Path) -> int:
    return int((fake_host / "state" / "attempts").read_text())


def _sleeps(fake_host: Path) -> list[int]:
    sleeps = fake_host / "state" / "sleeps"
    return [int(line) for line in sleeps.read_text().split()] if sleeps.exists() else []


def test_a_dropped_connection_is_retried_and_the_script_arrives_whole(fake_host: Path):
    script = "echo written\n"

    result = _run(fake_host, script, "drop,run")

    assert result.returncode == 0, result.stderr
    assert _attempts(fake_host) == 2
    assert (fake_host / "state" / "stdin.2").read_text() == script
    # Only the attempt that counted reaches stdout, so a redirected read-back is whole.
    assert result.stdout == "written\n"
    assert _sleeps(fake_host) == [10]


def test_the_retry_is_bounded(fake_host: Path):
    result = _run(fake_host, "echo never\n", "drop,drop,drop,drop,drop")

    assert result.returncode == 255
    assert _attempts(fake_host) == MAX_ATTEMPTS
    assert _sleeps(fake_host) == [10, 20], "linear backoff between attempts, none after the last"
    assert "failed 3 times; giving up" in result.stderr


@pytest.mark.parametrize("exit_code", [1, 3])
def test_a_script_that_fails_on_its_merits_is_not_retried(fake_host: Path, exit_code: int):
    result = _run(fake_host, f"echo refused >&2\nexit {exit_code}\n", "run,run,run")

    assert result.returncode == exit_code
    assert _attempts(fake_host) == 1
    assert _sleeps(fake_host) == []


def test_a_script_exiting_255_is_not_mistaken_for_a_dropped_connection(fake_host: Path):
    result = _run(fake_host, "exit 255\n", "run,run,run")

    assert result.returncode == 1
    assert _attempts(fake_host) == 1
    assert "remote script exited 255; reported as 1" in result.stderr


def test_a_script_failing_after_a_dropped_connection_is_not_retried_again(fake_host: Path):
    result = _run(fake_host, "exit 4\n", "drop,run,run")

    assert result.returncode == 4
    assert _attempts(fake_host) == 2


def test_secrets_travel_on_stdin_never_as_arguments(fake_host: Path):
    secret = "POSTGRES_PASSWORD=hunter2-not-real"  # noqa: S105 -- a fake value
    script = f"cat > /dev/null << 'ENVEOF'\n{secret}\nENVEOF\n"

    result = _run(fake_host, script, "run")

    assert result.returncode == 0, result.stderr
    argv = (fake_host / "state" / "argv.1").read_text()
    assert "hunter2" not in argv
    assert "not-a-real-key" not in argv
    assert "deploy@deploy.example" in argv
    assert "BatchMode=yes" in argv
    assert (fake_host / "state" / "key.1").read_text() == PRIVATE_KEY + "\n"
    assert (fake_host / "state" / "key-mode.1").read_text().strip() == "600"
    # The key file does not outlive the helper.
    assert not Path((fake_host / "state" / "key-path").read_text().strip()).exists()
