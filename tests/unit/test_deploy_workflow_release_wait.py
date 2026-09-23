"""The deploy waits for its releases before the host, and survives one SSH timeout.

Two properties of `.github/workflows/deploy.yml`:

- the release check (`scripts/wait_release.py`, worker and service chains) runs on the
  runner, for both
  contours, after the secret validations and before any step that touches the host;
- every file-only step reaches the host through `infra/scripts/deploy-ssh.sh`, whose
  retry is bounded and retries only a dropped connection (proved against the helper
  itself in test_deploy_ssh_retry.py). Steps that build, deploy, migrate or check
  health keep their single-shot ssh action.

The file-only steps are also rendered with placeholder values and run for real against
a fake `ssh` that executes the remote script in a local shell. That is what proves the
nested heredocs survive the move from an action input to a `run:` block: the `.env`
lands with every line intact, and a refusal on the host fails the step once.
"""

from pathlib import Path
import re
import stat
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
HELPER_CALL = "bash infra/scripts/deploy-ssh.sh"
RELEASE_WAIT_STEP = "Wait for this revision's worker and service releases"
FILE_ONLY_STEPS = (
    "Write .env to server",
    "Verify the deployed contour carries only its own credentials",
    "Record deployed revision and verified image digests",
)
# The Switch carries the GitHub App key, so its script travels on stdin through the same
# helper, but with a single attempt: it must never run twice at once on the host.
SWITCH_STEP = "Switch"
SINGLE_SHOT_STEPS = (
    "Pull and verify this revision's worker and service releases",
    "Reconcile managed deploy targets",
    "Cleanup",
)
DEPLOYED_SHA = "0123456789abcdef0123456789abcdef01234567"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _job() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]


def _steps() -> dict[str, dict]:
    return {step["name"]: step for step in _job()["steps"]}


def _names() -> list[str]:
    return [step["name"] for step in _job()["steps"]]


def _touches_host(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("appleboy/ssh-action") or HELPER_CALL in (
        step.get("run") or ""
    )


# --- the release wait ---


def test_the_release_is_checked_before_anything_touches_the_host():
    names = _names()
    steps = _steps()
    wait = names.index(RELEASE_WAIT_STEP)
    first_host_step = min(i for i, name in enumerate(names) if _touches_host(steps[name]))

    assert wait < first_host_step
    assert names[first_host_step] == "Write .env to server"
    for validation in (
        "Validate required secrets",
        "Validate production provider secrets",
        "Verify this contour's provider allowlist is its own",
    ):
        assert names.index(validation) < wait


def test_the_release_wait_runs_for_both_contours_on_the_runner():
    step = _steps()[RELEASE_WAIT_STEP]

    assert "if" not in step, "both contours deploy worker releases"
    assert "uses" not in step
    assert "python3 scripts/wait_release.py" in step["run"]
    assert '--revision "${DEPLOY_REVISION}"' in step["run"]
    assert "--chain worker --chain service" in step["run"]
    assert '--timeout-seconds "${RELEASE_WAIT_SECONDS}"' in step["run"]
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"  # noqa: S105
    assert step["env"]["GHCR_TOKEN"] == "${{ secrets.GHCR_TOKEN || github.token }}"  # noqa: S105


def test_the_wait_is_bounded_and_named_in_the_workflow():
    job = _job()
    seconds = int(job["env"]["RELEASE_WAIT_SECONDS"])

    assert seconds == 45 * 60
    # The step's own timeout is a backstop above the script's deadline, never below it.
    assert _steps()[RELEASE_WAIT_STEP]["timeout-minutes"] * 60 > seconds


def test_the_job_may_read_the_ci_run_it_waits_for():
    assert _job()["permissions"] == {"contents": "read", "packages": "read", "actions": "read"}


# --- the SSH retry ---


@pytest.mark.parametrize("name", (*FILE_ONLY_STEPS, SWITCH_STEP))
def test_every_file_only_step_goes_through_the_retrying_helper(name: str):
    step = _steps()[name]
    script = step["run"]

    assert "uses" not in step
    assert script.count(HELPER_CALL) == 1
    assert step["env"]["SSH_PRIVATE_KEY"] == "${{ secrets.SSH_PRIVATE_KEY }}"
    assert step["env"]["PROD_HOST"] == "${{ secrets.PROD_HOST }}"
    call_line = next(line for line in script.splitlines() if HELPER_CALL in line)
    assert call_line.rstrip().endswith("<<'REMOTE'"), "the remote script is a quoted heredoc"


@pytest.mark.parametrize("name", (*FILE_ONLY_STEPS, SWITCH_STEP))
def test_secrets_only_reach_the_remote_script_on_stdin(name: str):
    """Everything before the heredoc runs on the runner command line; no secret is there."""
    script = _steps()[name]["run"]
    before_heredoc = script.split(HELPER_CALL, 1)[0]

    assert "secrets." not in before_heredoc
    assert "REMOTE" in script.splitlines(), "the heredoc is closed at column 0"


def test_the_switch_is_never_retried():
    """A dropped connection may leave the first run going; a second must not start."""
    step = _steps()[SWITCH_STEP]

    assert str(step["env"]["DEPLOY_SSH_ATTEMPTS"]) == "1"
    assert all(
        "DEPLOY_SSH_ATTEMPTS" not in _steps()[name].get("env", {}) for name in FILE_ONLY_STEPS
    )


def test_no_step_calls_ssh_without_the_helper():
    offenders = [
        name
        for name, step in _steps().items()
        if re.search(r"^\s*ssh\s", step.get("run") or "", re.MULTILINE)
    ]

    assert not offenders, f"these steps open an ssh connection with no retry: {offenders}"


@pytest.mark.parametrize("name", SINGLE_SHOT_STEPS)
def test_steps_that_change_the_host_keep_their_single_shot_behaviour(name: str):
    step = _steps()[name]

    assert step["uses"] == "appleboy/ssh-action@v1"
    assert HELPER_CALL not in step["with"]["script"]


# --- the file-only steps, rendered and run ---

FAKE_SSH = """#!/usr/bin/env bash
set -uo pipefail
echo call >> "${FAKE_SSH_DIR}/calls"
remote="${@: -1}"
sh -c "${remote}"
"""


def _render(script: str, deploy_path: Path) -> str:
    def value(match: re.Match) -> str:
        expression = match.group(1)
        if expression == "env.DEPLOY_PATH":
            return str(deploy_path)
        if expression == "env.RELEASE_PENDING":
            return ".release-pending"
        if expression in ("github.sha", "env.DEPLOY_REVISION"):
            return DEPLOYED_SHA
        if expression.startswith("inputs.environment == 'production'"):
            return ""  # the stand contour
        if expression == "vars.MANAGED_SERVER_IDS_DECLARED":
            return "stand-ids"
        if expression == "secrets.TIME4VPS_MANAGED_SERVER_IDS":
            return "stand-ids"
        name = expression.split(".", 1)[1]
        return f"value-of-{name}"

    return EXPRESSION.sub(value, script)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_SSH)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "deploy").mkdir()
    return tmp_path


def _run_step(name: str, host: Path) -> subprocess.CompletedProcess[str]:
    script = _render(_steps()[name]["run"], host / "deploy")
    return subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{host / 'bin'}:/usr/bin:/bin",
            "FAKE_SSH_DIR": str(host),
            "SSH_PRIVATE_KEY": "not-a-real-key",
            "PROD_HOST": "deploy.example",
            "DEPLOY_SSH_USER": "deploy",
            "GITHUB_STEP_SUMMARY": str(host / "summary.md"),
            "RECORDS": str(host / "records"),
            "WORKER_DIGEST_FILE": str(host / "worker-record.json"),
            "SERVICE_DIGEST_FILE": str(host / "service-record.json"),
            "DEPLOY_REVISION": DEPLOYED_SHA,
        },
    )


def _calls(host: Path) -> int:
    return len((host / "calls").read_text().split())


def test_the_env_file_is_written_whole_and_private(host: Path):
    result = _run_step("Write .env to server", host)

    assert result.returncode == 0, result.stderr
    env_file = host / "deploy" / ".env"
    lines = env_file.read_text().splitlines()
    assert "POSTGRES_PASSWORD=value-of-POSTGRES_PASSWORD" in lines
    assert "TELEGRAM_BOT_TOKEN=" in lines
    assert "GITHUB_APP_PRIVATE_KEY_PATH=/app/keys/github_app.pem" in lines
    assert all(not line.startswith(" ") for line in lines), "heredoc indentation leaked"
    assert "ENVEOF" not in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert _calls(host) == 1


def test_the_written_env_passes_its_contour_check(host: Path):
    assert _run_step("Write .env to server", host).returncode == 0

    result = _run_step("Verify the deployed contour carries only its own credentials", host)

    assert result.returncode == 0, result.stderr
    assert "carries only its own allowlist: stand-ids" in result.stdout


def test_a_contour_check_that_refuses_fails_once_and_is_not_retried(host: Path):
    (host / "deploy" / ".env").write_text("TELEGRAM_BOT_TOKEN=production-token\n")

    result = _run_step("Verify the deployed contour carries only its own credentials", host)

    assert result.returncode == 1
    assert "TELEGRAM_BOT_TOKEN must be empty outside production" in result.stderr
    assert _calls(host) == 1


def test_the_records_are_read_back_into_the_summary(host: Path):
    """The pending records the verify step left, read before the Switch."""
    worker = '{\n  "git_sha": "abc",\n  "images": {}\n}\n'
    service = '{\n  "git_sha": "abc",\n  "schema_version": 1\n}\n'
    pending = host / "deploy" / ".release-pending"
    pending.mkdir()
    (pending / "deployed-worker-images.json").write_text(worker)
    (pending / "deployed-service-images.json").write_text(service)

    result = _run_step("Record deployed revision and verified image digests", host)

    assert result.returncode == 0, result.stderr
    assert (host / "worker-record.json").read_text() == worker
    assert (host / "service-record.json").read_text() == service
    summary = (host / "summary.md").read_text()
    assert DEPLOYED_SHA in summary
    assert '"images": {}' in summary
    assert '"schema_version": 1' in summary
    assert _calls(host) == 1


def test_a_missing_service_record_fails_the_record_step(host: Path):
    pending = host / "deploy" / ".release-pending"
    pending.mkdir()
    (pending / "deployed-worker-images.json").write_text('{"git_sha": "abc"}\n')

    result = _run_step("Record deployed revision and verified image digests", host)

    assert result.returncode != 0
