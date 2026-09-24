"""The deploy's Cleanup step: best-effort, reported, and never the reason a deploy is red.

Held down over the real `.github/workflows/deploy.yml`. The step's `run` block is rendered
and run as the runner runs it (`bash -eo pipefail`), through the real
`infra/scripts/deploy-ssh.sh`, with `ssh` replaced by a local shell and the deploy path's
cleanup commands replaced by ones that exit as a test asks.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/deploy.yml"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

FAKE_SSH = """#!/usr/bin/env bash
# The host: runs the remote command locally, reading the remote script on stdin.
if [ -n "${FAKE_SSH_STATUS:-}" ]; then
    echo "ssh: connect to host: Connection timed out" >&2
    exit "${FAKE_SSH_STATUS}"
fi
exec bash -c "${@: -1}"
"""
FAKE_COMMAND = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "${FAKE_CALLS}"
exit "${%s:-0}"
"""


def _cleanup_step() -> dict:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    return next(step for step in steps if step["name"] == "Cleanup")


def _cleanup_script() -> str:
    return _cleanup_step()["run"]


class Runner:
    """The runner's workspace and a deploy path on a local "host"."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.deploy_path = root / "deploy"
        self.summary = root / "summary.md"
        self.calls = root / "calls.log"
        (self.workspace / "infra/scripts").mkdir(parents=True)
        (self.workspace / "infra/scripts/deploy-ssh.sh").write_text(
            (REPO_ROOT / "infra/scripts/deploy-ssh.sh").read_text()
        )
        (self.deploy_path / "scripts").mkdir(parents=True)
        # The deploy path's cleanup commands, each exiting as FAKE_<NAME>_EXIT says.
        for name, variable in (
            ("cleanup_worker_images.py", "FAKE_WORKER_EXIT"),
            ("service_release.py", "FAKE_SERVICE_EXIT"),
        ):
            (self.deploy_path / "scripts" / name).write_text(
                "import os, pathlib, sys\n"
                f"with open(os.environ['FAKE_CALLS'], 'a') as calls:\n"
                f"    calls.write('{name} ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                f"sys.exit(int(os.environ.get('{variable}', '0')))\n"
            )
        binaries = root / "bin"
        binaries.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("docker", FAKE_COMMAND % "FAKE_DOCKER_EXIT")):
            (binaries / name).write_text(body)
            (binaries / name).chmod(0o755)
        self.path = f"{binaries}:{os.environ['PATH']}"

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        values = {"env.DEPLOY_PATH": str(self.deploy_path)}
        script = EXPRESSION.sub(lambda match: values[match.group(1)], _cleanup_script())
        (self.root / "step.sh").write_text(script)
        env = {
            "PATH": self.path,
            "HOME": str(self.root),
            "SSH_PRIVATE_KEY": "test-key",
            "PROD_HOST": "deploy-host.invalid",
            "DEPLOY_SSH_USER": "deploy",
            "DEPLOY_SSH_ATTEMPTS": "1",
            "CLEANUP_LOG": str(self.root / "cleanup.log"),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "FAKE_CALLS": str(self.calls),
            **overrides,
        }
        self.summary.write_text("")
        self.calls.write_text("")
        # How GitHub runs a `run` block with the default bash shell.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(self.root / "step.sh")],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def warnings(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("::warning")]

    def called(self) -> list[str]:
        return [line.split()[0] for line in self.calls.read_text().splitlines()]


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def test_a_cleanup_that_succeeds_says_so_and_warns_nothing(runner: Runner):
    result = runner.run()

    assert result.returncode == 0, result.stderr
    assert runner.warnings(result) == []
    assert runner.called() == ["cleanup_worker_images.py", "service_release.py", "docker"]
    assert "every command succeeded" in runner.summary.read_text()


def test_a_failed_cleanup_is_a_warning_naming_the_script_and_code_and_the_step_succeeds(
    runner: Runner,
):
    result = runner.run(FAKE_WORKER_EXIT="1", FAKE_SERVICE_EXIT="2", FAKE_DOCKER_EXIT="1")

    assert result.returncode == 0, result.stderr
    # Every command still ran after the one before it failed.
    assert runner.called() == ["cleanup_worker_images.py", "service_release.py", "docker"]
    warnings = runner.warnings(result)
    assert len(warnings) == 3, result.stdout
    for failure in (
        "scripts/cleanup_worker_images.py exit=1",
        "scripts/service_release.py cleanup exit=2",
        "docker image prune exit=1",
    ):
        assert any(failure in warning for warning in warnings), failure
        assert f"- {failure}" in runner.summary.read_text()


def test_an_unreachable_host_is_a_warning_and_the_step_succeeds(runner: Runner):
    result = runner.run(FAKE_SSH_STATUS="255")

    assert result.returncode == 0, result.stderr
    assert runner.called() == []
    (warning,) = runner.warnings(result)
    assert "the cleanup session on the host exit=255" in warning
    assert "- the cleanup session on the host exit=255" in runner.summary.read_text()


def test_nothing_the_cleanup_step_does_fails_the_job():
    """Even a timeout: the step may fail, the job does not."""
    step = _cleanup_step()

    assert step["continue-on-error"] is True
    assert step["timeout-minutes"] <= 10


def test_cleanup_prunes_dangling_images_after_both_release_cleanups():
    script = _cleanup_script()

    worker_cleanup = script.index("python3 scripts/cleanup_worker_images.py")
    service_cleanup = script.index("python3 scripts/service_release.py cleanup")
    dangling_image_prune = script.index("docker image prune -f")

    assert worker_cleanup < service_cleanup < dangling_image_prune


def test_cleanup_prunes_no_build_cache_because_the_host_builds_nothing():
    assert "builder prune" not in _cleanup_script()
