"""The CI infrastructure-failure marker, run the way ci.yml runs it.

Every script here is the one the workflow holds: the step's `run` is read out of
ci.yml or the local action, its `${{ }}` expressions are filled in from the case, and
it runs under bash with fakes for pip, docker and make on PATH. So a test fails when
the workflow drifts from the helper, not only when the helper breaks.
"""

import json
from pathlib import Path
import re
import subprocess
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "ci-infra.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTIONS = ROOT / ".github" / "actions"
MARKER = re.compile(
    r"^CI-INFRA-FAILURE: job=[A-Za-z0-9._/-]+ step=[A-Za-z0-9._/-]+ cause=[A-Za-z0-9._/-]+$"
)
ANNOTATION = "::error title=CI infrastructure failure::"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job, *, name=None, step_id=None):
    for step in job["steps"]:
        if (name is not None and step.get("name") == name) or (
            step_id is not None and step.get("id") == step_id
        ):
            return step
    raise AssertionError(f"no step {name or step_id}")


def _render(text, context):
    """text with every ${{ expression }} replaced; an unknown expression is an error."""
    return EXPRESSION.sub(lambda match: context[match.group(1)], text)


class Runner:
    """A GitHub runner's files and environment, with fakes first on PATH."""

    def __init__(self, tmp_path):
        self.temp = tmp_path / "runner-temp"
        self.temp.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.summary = tmp_path / "summary.md"
        self.output = tmp_path / "output"
        self.calls = tmp_path / "calls"
        self.summary.touch()
        self.output.touch()
        self.calls.touch()
        self.github_env = tmp_path / "github-env"
        self.github_env.touch()
        self.env = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_WORKSPACE": str(ROOT),
            "GITHUB_JOB": "fast-checks",
            "CI_INFRA_RETRY_DELAY": "0",
            "FAKE_CALLS": str(self.calls),
        }

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$FAKE_CALLS"\n{body}\n')
        path.chmod(0o755)

    def run(self, script, **env):
        return subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=ROOT,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def helper(self, *args, **env):
        return self.run(" ".join(["bash", "scripts/ci-infra.sh", *args]), **env)

    def call_lines(self, prefix):
        return [line for line in self.calls.read_text().splitlines() if line.startswith(prefix)]

    def markers(self):
        """The markers this job wrote, read back from the summary."""
        return [line for line in self.summary.read_text().splitlines() if MARKER.match(line)]

    def outputs(self):
        """GITHUB_OUTPUT parsed, heredoc form included."""
        values, lines = {}, iter(self.output.read_text().splitlines())
        for line in lines:
            name, _, delimiter = line.partition("<<")
            body = []
            for body_line in lines:
                if body_line == delimiter:
                    break
                body.append(body_line)
            values[name] = "\n".join(body) + "\n"
        return values


@pytest.fixture
def runner(tmp_path):
    return Runner(tmp_path)


def _assert_one_marker(result, runner, marker):
    assert MARKER.match(marker)
    assert f"{ANNOTATION}{marker}" in result.stdout.splitlines()
    assert runner.markers() == [marker]
    assert (runner.temp / "ci-infra-markers").read_text() == f"{marker}\n"


# --- the helper ------------------------------------------------------------


def test_mark_writes_one_marker_to_annotation_summary_and_job_file(runner):
    result = runner.helper("mark", "--step", "install-uv", "--cause", "uv-download")

    assert result.returncode == 0, result.stderr
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=install-uv cause=uv-download"
    )


def test_mark_names_the_matrix_leg_when_the_job_sets_one(runner):
    result = runner.helper(
        "mark", "--step", "pull-images", "--cause", "image-pull", CI_INFRA_JOB="test-service/api"
    )

    assert result.returncode == 0, result.stderr
    assert runner.markers() == [
        "CI-INFRA-FAILURE: job=test-service/api step=pull-images cause=image-pull"
    ]


def test_a_field_that_would_break_the_format_is_refused(runner):
    result = runner.helper("mark", "--step", "'two words'", "--cause", "uv-download")

    assert result.returncode == 2
    assert runner.markers() == []


def test_retry_that_recovers_writes_no_marker(runner):
    runner.fake(
        "flaky",
        '[ "$(grep -c "^flaky" "$FAKE_CALLS")" -ge 2 ] || exit 1',
    )

    result = runner.helper("retry", "--step", "s", "--cause", "c", "--", "flaky")

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(runner.call_lines("flaky")) == 2
    assert runner.markers() == []


def test_retry_gives_up_after_three_attempts_with_the_command_status(runner):
    runner.fake("down", "exit 7")

    result = runner.helper("retry", "--step", "download", "--cause", "registry", "--", "down")

    assert result.returncode == 7
    assert len(runner.call_lines("down")) == 3
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=download cause=registry"
    )


def test_retry_stops_an_attempt_that_hangs_and_runs_the_next(runner):
    """A hung attempt is a failed attempt: the bound stops it and the retry goes on."""
    runner.fake(
        "hangs-once",
        '[ "$CI_INFRA_ATTEMPT" = 1 ] && exec sleep 60\nexit 0',
    )

    result = runner.helper(
        "retry", "--step", "s", "--cause", "c", "--attempt-timeout", "1s", "--", "hangs-once"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(runner.call_lines("hangs-once")) == 2
    assert "attempt 1 of 3 timed out after 1s" in result.stdout
    assert runner.markers() == []


def test_retry_whose_last_attempt_timed_out_names_the_timeout(runner):
    runner.fake("hangs", "exec sleep 60")

    result = runner.helper(
        "retry", "--step", "dl", "--cause", "registry", "--attempt-timeout", "1", "--", "hangs"
    )

    assert result.returncode == 124
    assert len(runner.call_lines("hangs")) == 3
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=dl cause=registry-timeout"
    )


def test_bound_stops_a_hung_command_with_its_children_and_names_the_timeout(runner):
    runner.fake("hangs", 'sleep 60 &\necho "$!" > "$RUNNER_TEMP/child"\nwait')

    result = runner.helper("bound", "--step", "service-tests", "--timeout", "1s", "--", "hangs")

    assert result.returncode == 124
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=service-tests cause=step-timeout"
    )
    assert "ran past its bound of 1s" in runner.summary.read_text()
    assert _stopped(int((runner.temp / "child").read_text()))


def _stopped(pid, *, within=5.0):
    """Whether pid is gone within a few seconds; a zombie not yet reaped counts as gone."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("body,status", [("exit 0", 0), ("exit 5", 5)])
def test_bound_leaves_a_command_that_finished_in_time_unmarked(runner, body, status):
    """A pass passes, and a failure inside the bound stays the command's own."""
    runner.fake("quick", body)

    result = runner.helper("bound", "--step", "service-tests", "--timeout", "30s", "--", "quick")

    assert result.returncode == status
    assert runner.markers() == []


@pytest.mark.parametrize("status", [124, 137])
def test_bound_does_not_take_a_command_own_timeout_status_for_its_bound(runner, status):
    """124 and 137 are what timeout exits with, but a command can exit so itself: only
    the bound's own timer names a step-timeout, and the command's status is kept."""
    runner.fake("quick", f"exit {status}")

    result = runner.helper("bound", "--step", "service-tests", "--timeout", "30s", "--", "quick")

    assert result.returncode == status
    assert runner.markers() == []
    assert "ran past its bound" not in result.stdout


def test_retry_counts_a_command_own_status_124_as_a_failure_not_a_timeout(runner):
    runner.fake("quick", "exit 124")

    result = runner.helper(
        "retry", "--step", "dl", "--cause", "registry", "--attempt-timeout", "30s", "--", "quick"
    )

    assert result.returncode == 124
    assert "attempt 1 of 3 failed with status 124" in result.stdout
    assert "timed out" not in result.stdout
    _assert_one_marker(result, runner, "CI-INFRA-FAILURE: job=fast-checks step=dl cause=registry")


@pytest.mark.parametrize("duration", ["0", "10x", "1.5m", "-1"])
def test_a_duration_timeout_cannot_read_is_refused(runner, duration):
    runner.fake("quick", "exit 0")

    result = runner.helper("bound", "--step", "s", "--timeout", duration, "--", "quick")

    assert result.returncode == 2
    assert runner.call_lines("quick") == []


def test_watch_marks_a_failure_whose_output_names_a_known_cause(runner):
    runner.fake(
        "build",
        'echo "Exit: Failed to build worker-base-claude"\n'
        'echo "CI-INFRA-CAUSE=claude-installer-fetch !!!"\nexit 3',
    )

    result = runner.helper("watch", "--step", "integration-tests", "--", "build")

    assert result.returncode == 3
    assert "Failed to build worker-base-claude" in result.stdout
    _assert_one_marker(
        result,
        runner,
        "CI-INFRA-FAILURE: job=fast-checks step=integration-tests cause=claude-installer-fetch",
    )


@pytest.mark.parametrize(
    "body,status",
    [
        ('echo "AssertionError: expected 2"; exit 1', 1),
        ('echo "CI-INFRA-CAUSE=something-else"; exit 1', 1),
        ('echo "CI-INFRA-CAUSE=claude-installer-fetch"; exit 0', 0),
    ],
)
def test_watch_leaves_other_outcomes_unmarked(runner, body, status):
    """A test failure, an unknown cause, or a run that passed anyway: no marker."""
    runner.fake("build", body)

    result = runner.helper("watch", "--step", "integration-tests", "--", "build")

    assert result.returncode == status
    assert runner.markers() == []
    assert "CI-INFRA-FAILURE" not in result.stdout


def test_expose_writes_the_markers_as_a_step_output(runner):
    runner.helper("mark", "--step", "a", "--cause", "x")
    runner.helper("mark", "--step", "b", "--cause", "y")

    result = runner.helper("expose", "--output", "infra-marker-api")

    assert result.returncode == 0, result.stderr
    assert runner.outputs() == {
        "infra-marker-api": (
            "CI-INFRA-FAILURE: job=fast-checks step=a cause=x\n"
            "CI-INFRA-FAILURE: job=fast-checks step=b cause=y\n"
        )
    }


def test_expose_without_a_marker_writes_nothing(runner):
    result = runner.helper("expose", "--output", "infra-marker")

    assert result.returncode == 0, result.stderr
    assert runner.output.read_text() == ""


# --- (a)+(b): each listed download step retries, bounded, then marks -------

COMPOSE_CONFIG = json.dumps(
    {
        "services": {
            "api": {"build": {"context": "."}, "image": "codegen-orchestrator/api:test"},
            "api-factory": {"image": "codegen-orchestrator/api:test"},
            "db": {"image": "pgvector/pgvector:0.8.6-pg16"},
            "redis": {"image": "redis:7.4.10-alpine"},
            "runner": {"build": {"context": "."}},
        }
    }
)


def _fake_docker(runner, *, pull_status, pull_hangs=False):
    pull = "exec sleep 60" if pull_hangs else f"exit {pull_status}"
    runner.fake(
        "docker",
        f"""case "$1" in
  compose) printf '%s' '{COMPOSE_CONFIG}' ;;
  pull) {pull} ;;
  *) exit 99 ;;
esac""",
    )


@pytest.mark.parametrize("job_name", ["fast-checks", "ci-contract"])
def test_the_workflow_uv_install_retries_three_times_then_marks(runner, job_name):
    runner.fake("pip", "exit 1")
    script = _step(_jobs()[job_name], name="Install uv")["run"]

    result = runner.run(script, GITHUB_JOB=job_name)

    assert result.returncode != 0
    assert runner.call_lines("pip") == ["pip install uv"] * 3
    _assert_one_marker(
        result, runner, f"CI-INFRA-FAILURE: job={job_name} step=install-uv cause=uv-download"
    )


PULL_CASES = [
    ("test-service", {"matrix.service": "api"}, "test-service/api"),
    ("test-integration", {"matrix.suite": "po-tools"}, "test-integration/po-tools"),
    ("test-backend-dind-integration", {}, None),
]


@pytest.mark.parametrize("job_name,matrix,leg", PULL_CASES)
def test_the_workflow_image_pull_retries_three_times_then_marks(runner, job_name, matrix, leg):
    _fake_docker(runner, pull_status=1)
    job = _jobs()[job_name]
    script = _render(_step(job, step_id="pull-images")["run"], matrix)
    env = {"GITHUB_JOB": job_name}
    if leg is not None:
        env["CI_INFRA_JOB"] = _render(job["env"]["CI_INFRA_JOB"], matrix)
        assert env["CI_INFRA_JOB"] == leg

    result = runner.run(script, **env)

    assert result.returncode != 0
    # The first image fails all three attempts; the job stops there.
    assert runner.call_lines("docker pull") == ["docker pull pgvector/pgvector:0.8.6-pg16"] * 3
    _assert_one_marker(
        result, runner, f"CI-INFRA-FAILURE: job={leg or job_name} step=pull-images cause=image-pull"
    )


def test_a_hung_image_pull_is_retried_under_its_bound_then_named(runner):
    _fake_docker(runner, pull_status=0, pull_hangs=True)
    script = _render(
        _step(_jobs()["test-service"], step_id="pull-images")["run"], {"matrix.service": "api"}
    )

    result = runner.run(script, CI_INFRA_JOB="test-service/api", CI_INFRA_PULL_ATTEMPT_TIMEOUT="1s")

    assert result.returncode != 0
    assert runner.call_lines("docker pull") == ["docker pull pgvector/pgvector:0.8.6-pg16"] * 3
    _assert_one_marker(
        result,
        runner,
        "CI-INFRA-FAILURE: job=test-service/api step=pull-images cause=image-pull-timeout",
    )


def test_the_redis_pull_of_fast_checks_retries_under_a_bound_then_marks(runner):
    _fake_docker(runner, pull_status=1)
    script = _step(_jobs()["fast-checks"], name="Pull Redis image with retry")["run"]

    result = runner.run(script, GITHUB_JOB="fast-checks")

    assert result.returncode != 0
    assert runner.call_lines("docker pull") == ["docker pull redis:7.4.10-alpine"] * 3
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=redis-pull cause=image-pull"
    )


def test_the_image_pull_pulls_only_what_no_service_builds(runner):
    _fake_docker(runner, pull_status=0)
    script = _render(
        _step(_jobs()["test-service"], step_id="pull-images")["run"], {"matrix.service": "api"}
    )

    result = runner.run(script, CI_INFRA_JOB="test-service/api")

    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.call_lines("docker pull") == [
        "docker pull pgvector/pgvector:0.8.6-pg16",
        "docker pull redis:7.4.10-alpine",
    ]
    assert runner.markers() == []


def test_an_unreadable_compose_file_fails_without_a_marker(runner):
    """Reading the compose file is not a download: its failure is the code's."""
    runner.fake("docker", "exit 1")
    script = _render(
        _step(_jobs()["test-service"], step_id="pull-images")["run"], {"matrix.service": "api"}
    )

    result = runner.run(script, CI_INFRA_JOB="test-service/api")

    assert result.returncode != 0
    assert runner.call_lines("docker pull") == []
    assert runner.markers() == []


@pytest.mark.parametrize(
    "action,step,cause",
    [
        ("setup-uv-with-retry", "setup-uv", "uv-download"),
    ],
)
def test_a_retry_action_marks_only_when_every_attempt_failed(runner, action, step, cause):
    steps = yaml.safe_load((ACTIONS / action / "action.yml").read_text())["runs"]["steps"]
    attempts = [candidate for candidate in steps if "uses" in candidate]
    assert len(attempts) == 3
    assert all(attempt["continue-on-error"] is True for attempt in attempts)
    script = _step({"steps": steps}, name="Fail as CI infrastructure after retry exhaustion")["run"]

    recovered = {
        "steps.attempt-1.outcome": "failure",
        "steps.attempt-2.outcome": "failure",
        "steps.attempt-3.outcome": "success",
    }
    result = runner.run(_render(script, recovered), GITHUB_JOB="test-service")
    assert result.returncode == 0
    assert runner.markers() == []

    exhausted = {**recovered, "steps.attempt-3.outcome": "failure"}
    result = runner.run(_render(script, exhausted), GITHUB_JOB="test-service")
    assert result.returncode == 1
    _assert_one_marker(
        result, runner, f"CI-INFRA-FAILURE: job=test-service step={step} cause={cause}"
    )


# --- the Buildx bootstrap: every attempt bounded ------------------------------

BUILDX_ACTION = ACTIONS / "setup-buildx-with-retry"
# The action's real bound, and the one a test runs it with.
BUILDX_ATTEMPT_TIMEOUT = "--attempt-timeout 120s"
TEST_ATTEMPT_TIMEOUT = "--attempt-timeout 1s"


def _buildx_setup(runner, *, registry_failure="false", pull_hang="false"):
    """Run the action's bootstrap step the way the runner does, with a 1 s bound."""
    steps = yaml.safe_load((BUILDX_ACTION / "action.yml").read_text())["runs"]["steps"]
    assert [step["name"] for step in steps] == ["Set up Docker Buildx in bounded attempts"]
    step = steps[0]
    assert BUILDX_ATTEMPT_TIMEOUT in step["run"]
    context = {
        "inputs.simulate_first_attempt_registry_failure": registry_failure,
        "inputs.simulate_first_attempt_pull_hang": pull_hang,
    }
    env = {name: _render(value, context) for name, value in step["env"].items()}
    script = step["run"].replace(BUILDX_ATTEMPT_TIMEOUT, TEST_ATTEMPT_TIMEOUT)
    return runner.run(
        script, GITHUB_ACTION_PATH=str(BUILDX_ACTION), GITHUB_JOB="test-service", **env
    )


def _fake_buildx(runner, *, bootstrap):
    runner.fake(
        "docker",
        f"""case "$1 $2" in
  "buildx create") exit 0 ;;
  "buildx inspect") {bootstrap} ;;
  *) exit 99 ;;
esac""",
    )


# A push or pull_request run has no workflow_dispatch inputs: ci.yml's
# ${{ inputs.simulate_* }} is empty there, and job 107167875626 of run 35856811121 lost
# its first attempt to that before the empty value meant "not requested".
@pytest.mark.parametrize("simulation", ["", "false"])
def test_buildx_bootstraps_one_builder_and_names_it(runner, simulation):
    _fake_buildx(runner, bootstrap="exit 0")

    result = _buildx_setup(runner, registry_failure=simulation, pull_hang=simulation)

    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.call_lines("docker buildx") == [
        "docker buildx create --name ci-builder-1 --driver docker-container --buildkitd-flags "
        "--allow-insecure-entitlement security.insecure --allow-insecure-entitlement "
        "network.host --use",
        "docker buildx inspect --bootstrap ci-builder-1",
    ]
    assert runner.github_env.read_text() == "BUILDX_BUILDER=ci-builder-1\n"
    assert "retrying" not in result.stdout
    assert runner.markers() == []


def test_a_simulation_value_that_is_not_a_boolean_fails_every_attempt(runner):
    _fake_buildx(runner, bootstrap="exit 0")

    result = _buildx_setup(runner, pull_hang="yes")

    assert result.returncode == 2
    assert result.stderr.count("SIMULATE_PULL_HANG is 'yes'") == 3
    assert runner.call_lines("docker buildx") == []


def test_a_first_buildx_attempt_whose_pull_hangs_is_bounded_and_the_next_proceeds(runner):
    """The simulation input: attempt 1 hangs, its bound fails it, attempt 2 builds."""
    _fake_buildx(runner, bootstrap="exit 0")

    result = _buildx_setup(runner, pull_hang="true")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "the buildkit pull of Buildx attempt 1 hangs" in result.stdout
    assert "attempt 1 of 3 timed out after 1s" in result.stdout
    assert runner.call_lines("docker buildx inspect") == [
        "docker buildx inspect --bootstrap ci-builder-2"
    ]
    assert runner.github_env.read_text() == "BUILDX_BUILDER=ci-builder-2\n"
    assert runner.markers() == []


def test_a_first_buildx_attempt_that_fails_is_retried(runner):
    _fake_buildx(runner, bootstrap="exit 0")

    result = _buildx_setup(runner, registry_failure="true")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "attempt 1 of 3 failed with status 1" in result.stdout
    assert runner.call_lines("docker buildx inspect") == [
        "docker buildx inspect --bootstrap ci-builder-2"
    ]
    assert runner.markers() == []


@pytest.mark.parametrize(
    "bootstrap,status,cause",
    [("exit 1", 1, "buildx-registry"), ("exec sleep 60", 124, "buildx-registry-timeout")],
)
def test_buildx_that_never_boots_fails_the_step_and_marks(runner, bootstrap, status, cause):
    _fake_buildx(runner, bootstrap=bootstrap)

    result = _buildx_setup(runner)

    assert result.returncode == status
    assert len(runner.call_lines("docker buildx inspect")) == 3
    _assert_one_marker(
        result, runner, f"CI-INFRA-FAILURE: job=test-service step=setup-buildx cause={cause}"
    )


# --- docker steps: bounded inside the job -----------------------------------

BOUNDED_STEPS = [
    ("service-image-imports", "service-image-imports", {}, None, "python"),
    ("test-service", "service-tests", {"matrix.service": "api"}, "test-service/api", "make"),
    (
        "test-integration",
        "integration-tests",
        {"matrix.suite": "backend"},
        "test-integration/backend",
        "make",
    ),
    ("test-backend-dind-integration", "integration-tests", {}, None, "make"),
]
STEP_BOUND = re.compile(r"--timeout \S+ --")


@pytest.mark.parametrize("job_name,step_id,matrix,leg,program", BOUNDED_STEPS)
def test_a_docker_step_that_hangs_is_stopped_inside_the_job_and_named(
    runner, job_name, step_id, matrix, leg, program
):
    runner.fake(program, "exec sleep 60")
    job = _jobs()[job_name]
    run = _render(_step(job, step_id=step_id)["run"], matrix)
    assert len(STEP_BOUND.findall(run)) == 1
    env = {"GITHUB_JOB": job_name}
    if leg is not None:
        env["CI_INFRA_JOB"] = _render(job["env"]["CI_INFRA_JOB"], matrix)

    result = runner.run(STEP_BOUND.sub("--timeout 1s --", run), **env)

    assert result.returncode == 124
    _assert_one_marker(
        result, runner, f"CI-INFRA-FAILURE: job={leg or job_name} step={step_id} cause=step-timeout"
    )


def _fake_redis_docker(runner, *, run):
    runner.fake(
        "docker",
        f"""case "$1" in
  run) {run} ;;
  exec) echo PONG ;;
  rm) exit 0 ;;
  *) exit 99 ;;
esac""",
    )


def test_the_redis_regression_runs_its_container_test_and_cleanup_under_its_bound(runner):
    _fake_redis_docker(runner, run="exit 0")
    runner.fake("uv", 'echo "LIVE_REDIS_CONTAINER=$LIVE_REDIS_CONTAINER" >> "$FAKE_CALLS"')
    run = _step(_jobs()["fast-checks"], name="Run Redis capability cleanup regression")["run"]
    assert len(STEP_BOUND.findall(run)) == 1

    result = runner.run(run, GITHUB_JOB="fast-checks")

    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.calls.read_text().splitlines() == [
        "docker run --detach --rm --name redis-cleanup-contract redis:7.4.10-alpine",
        "docker exec redis-cleanup-contract redis-cli --raw ping",
        "docker exec redis-cleanup-contract redis-cli --raw ping",
        "uv run pytest -q tests/live/test_capability_cleanup_redis.py",
        "LIVE_REDIS_CONTAINER=redis-cleanup-contract",
        "docker rm --force redis-cleanup-contract",
    ]
    assert runner.markers() == []


def test_a_redis_run_that_hangs_is_stopped_inside_fast_checks_and_named(runner):
    _fake_redis_docker(runner, run="exec sleep 60")
    run = _step(_jobs()["fast-checks"], name="Run Redis capability cleanup regression")["run"]

    result = runner.run(STEP_BOUND.sub("--timeout 1s --", run), GITHUB_JOB="fast-checks")

    assert result.returncode == 124
    _assert_one_marker(
        result, runner, "CI-INFRA-FAILURE: job=fast-checks step=redis-cleanup cause=step-timeout"
    )


def test_a_hung_publish_names_itself_on_its_own_job(runner):
    """publish-worker-images runs after the gate: its own expose step carries the marker."""
    runner.fake("publish", "exec sleep 60")
    job = _jobs()["publish-worker-images"]
    run = _step(job, name="Build and publish the worker chain")["run"]
    publish = "bash infra/scripts/publish-worker-images.sh"
    assert run.count(publish) == 1

    result = runner.run(
        STEP_BOUND.sub("--timeout 1s --", run).replace(publish, "publish"),
        GITHUB_JOB="publish-worker-images",
    )

    assert result.returncode == 124
    _assert_one_marker(
        result,
        runner,
        "CI-INFRA-FAILURE: job=publish-worker-images step=publish cause=step-timeout",
    )
    expose = _step(job, name="Expose CI infrastructure failure")
    assert expose is job["steps"][-1] and expose["if"].startswith("always()")
    assert runner.run(expose["run"], GITHUB_JOB="publish-worker-images").returncode == 0
    assert runner.outputs() == {
        "infra-marker": (
            "CI-INFRA-FAILURE: job=publish-worker-images step=publish cause=step-timeout\n"
        )
    }
    assert job["outputs"]["infra-marker"] == "${{ steps.infra.outputs['infra-marker'] }}"


def test_a_hung_step_travels_through_the_gate_and_the_gate_stays_red(runner):
    """The always() expose step hands the step-timeout marker to the gate; it fails."""
    runner.fake("make", "exec sleep 60")
    job = _jobs()["test-service"]
    run = _render(_step(job, step_id="service-tests")["run"], {"matrix.service": "api"})
    runner.run(STEP_BOUND.sub("--timeout 1s --", run), CI_INFRA_JOB="test-service/api")
    expose = _step(job, name="Expose CI infrastructure failure")
    assert expose["if"].startswith("always()")
    exposed = runner.run(_render(expose["run"], {"matrix.service": "api"}))
    assert exposed.returncode == 0
    outputs = runner.outputs()

    runner.summary.write_text("")
    result = _run_gate(runner, {"test-service": "failure"}, {"test-service": outputs})

    assert result.returncode == 1
    assert "Required CI gate failed" in result.stdout
    assert runner.markers() == [
        "CI-INFRA-FAILURE: job=test-service/api step=service-tests cause=step-timeout"
    ]


def test_the_dind_suite_marks_an_exhausted_claude_installer_fetch(runner):
    runner.fake(
        "make",
        'echo "!!! _pytest.outcomes.Exit: Failed to build worker-base-claude:abc"\n'
        'echo "CI-INFRA-CAUSE=claude-installer-fetch !!!"\nexit 2',
    )
    script = _step(_jobs()["test-backend-dind-integration"], step_id="integration-tests")["run"]

    result = runner.run(script, GITHUB_JOB="test-backend-dind-integration")

    assert result.returncode == 2
    assert runner.call_lines("make") == ["make test-integration-backend-dind"]
    _assert_one_marker(
        result,
        runner,
        "CI-INFRA-FAILURE: job=test-backend-dind-integration step=integration-tests "
        "cause=claude-installer-fetch",
    )


def test_a_failing_dind_test_stays_a_product_failure(runner):
    runner.fake("make", 'echo "FAILED tests/integration/backend/test_dev_env.py"; exit 2')
    script = _step(_jobs()["test-backend-dind-integration"], step_id="integration-tests")["run"]

    result = runner.run(script, GITHUB_JOB="test-backend-dind-integration")

    assert result.returncode == 2
    assert runner.markers() == []


# --- (c)(d)(e): the Required CI Gate ----------------------------------------

GATE_NEEDS = [
    "detect-changes",
    "fast-checks",
    "ci-contract",
    "service-image-imports",
    "test-service",
    "test-integration",
    "template-compatibility",
    "web-checks",
    "test-backend-dind-integration",
]


def _run_gate(runner, results, outputs=None):
    outputs = outputs or {}
    needs = {
        job: {"result": results.get(job, "success"), "outputs": outputs.get(job, {})}
        for job in GATE_NEEDS
    }
    gate = _step(_jobs()["merge-gate"], name="Check required jobs")
    context = {f"needs.{job}.result": needs[job]["result"] for job in GATE_NEEDS}
    script = _render(gate["run"], context)
    assert gate["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
    return runner.run(
        script,
        NEEDS_JSON=json.dumps(needs, indent=2),
        GITHUB_REF="refs/pull/1/merge",
        GITHUB_JOB="merge-gate",
    )


def test_the_gate_passes_when_everything_succeeded(runner):
    result = _run_gate(
        runner, {"web-checks": "skipped", "test-backend-dind-integration": "skipped"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI-INFRA-FAILURE" not in result.stdout + runner.summary.read_text()


def test_the_gate_fails_and_carries_the_marker_of_an_infra_failure(runner):
    marker = "CI-INFRA-FAILURE: job=test-integration/po-tools step=pull-images cause=image-pull"

    result = _run_gate(
        runner,
        {"test-integration": "failure"},
        {"test-integration": {"infra-marker-po-tools": f"{marker}\n"}},
    )

    assert result.returncode == 1
    assert f"{ANNOTATION}{marker}" in result.stdout.splitlines()
    assert "Required CI gate failed" in result.stdout
    assert runner.markers() == [marker]


def test_the_gate_repeats_every_leg_and_job_that_failed_on_infra(runner):
    first = "CI-INFRA-FAILURE: job=test-service/api step=setup-buildx cause=buildx-registry"
    second = "CI-INFRA-FAILURE: job=test-service/infra step=pull-images cause=image-pull"
    third = "CI-INFRA-FAILURE: job=ci-contract step=install-uv cause=uv-download"

    result = _run_gate(
        runner,
        {"test-service": "failure", "ci-contract": "failure"},
        {
            "test-service": {"infra-marker-api": f"{first}\n", "infra-marker-infra": f"{second}\n"},
            "ci-contract": {"infra-marker": f"{third}\n"},
        },
    )

    assert result.returncode == 1
    assert sorted(runner.markers()) == sorted([first, second, third])


def test_the_gate_fails_without_a_marker_on_a_product_failure(runner):
    result = _run_gate(runner, {"test-service": "failure"})

    assert result.returncode == 1
    assert "Required CI gate failed" in result.stdout
    assert "CI-INFRA-FAILURE" not in result.stdout + runner.summary.read_text()


def test_the_gate_never_passes_on_a_marker(runner):
    """Even a marker next to a failure that is all infra leaves the verdict red."""
    marker = "CI-INFRA-FAILURE: job=fast-checks step=install-uv cause=uv-download"

    result = _run_gate(
        runner,
        {"fast-checks": "failure", "test-service": "skipped", "test-integration": "skipped"},
        {"fast-checks": {"infra-marker": f"{marker}\n"}},
    )

    assert result.returncode == 1
    assert runner.markers() == [marker]


def test_a_marker_travels_from_the_failing_job_through_the_gate(runner):
    """The job's expose output, handed over as needs.<job>.outputs, is what the gate repeats."""
    runner.fake("pip", "exit 1")
    runner.run(_step(_jobs()["ci-contract"], name="Install uv")["run"], GITHUB_JOB="ci-contract")
    expose = _step(_jobs()["ci-contract"], name="Expose CI infrastructure failure")["run"]
    assert runner.run(expose, GITHUB_JOB="ci-contract").returncode == 0
    outputs = runner.outputs()

    runner.summary.write_text("")
    result = _run_gate(runner, {"ci-contract": "failure"}, {"ci-contract": outputs})

    assert result.returncode == 1
    assert runner.markers() == [
        "CI-INFRA-FAILURE: job=ci-contract step=install-uv cause=uv-download"
    ]
