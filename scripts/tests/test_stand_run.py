"""What the stand runner must not forget — each of these cost a run to learn."""

import subprocess

import pytest

from scripts import clean_live_tests, stand_run
from scripts.stand_run import (
    AGENTS,
    BRIEF_HARD_STOP_SECONDS,
    BRIEF_PACKAGE_HARD_STOP_SECONDS,
    BRIEF_PACKAGE_RUNNER_TIMEOUT_SECONDS,
    BRIEF_PACKAGE_SUITE_TIMEOUT_SECONDS,
    BRIEF_RUNNER_TIMEOUT_SECONDS,
    BRIEF_SUITE_TIMEOUT_SECONDS,
    NOOP_SUITE_TIMEOUT_SECONDS,
    QA_EXECUTOR_ENV,
    STAND_JOB_RESERVE_SECONDS,
    STAND_JOB_TIMEOUT_MINUTES,
    STAND_PROVISIONING_TIMEOUT_SECONDS,
    STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS,
    SUITE_ALIASES,
    SUITES,
    Suite,
    compose_environment,
    matrix_row,
    qa_executor_services,
    read_env_file,
    resolve_suite,
    write_junit_report,
    write_qa_executor,
)
from shared import stand_deadlines


@pytest.fixture(autouse=True)
def _release_override(tmp_path_factory, monkeypatch):
    """Every run here is a stand run, and a stand run has its release override.

    The workflow names the override bring-up generated; a test that is about its
    absence removes it itself.
    """
    override = tmp_path_factory.mktemp("release") / "deployed-service-images.compose.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv(stand_run.SERVICE_RELEASE_OVERRIDE_ENV, str(override))
    return override


@pytest.fixture(autouse=True)
def _sweep_requirements(monkeypatch):
    """And its sweep is configured: the deployed `.env` carries the key and run tag.

    A test that is about a missing requirement removes it itself. `API_BASE_URL`
    is not among these: the runner forms it, and nothing exported may stand in.
    """
    monkeypatch.setenv(clean_live_tests.INTERNAL_API_KEY_ENV, "test-internal-key")
    monkeypatch.setenv(clean_live_tests.STAND_RUN_TAG_ENV, "gha-1-1")
    monkeypatch.delenv(clean_live_tests.API_BASE_URL_ENV, raising=False)


def test_compose_calls_drop_the_exported_qa_executor():
    """Compose reads the process environment before .env.

    A runner that sourced the deployed .env into its own environment therefore
    pins the executor it is trying to change: the recreated container comes back
    with the old value and the matrix silently runs the wrong half twice.
    """
    env = {QA_EXECUTOR_ENV: "claude", "INTERNAL_API_KEY": "k"}

    passed = compose_environment(env)

    assert QA_EXECUTOR_ENV not in passed
    assert passed["INTERNAL_API_KEY"] == "k"


def test_writing_the_executor_replaces_rather_than_appends(tmp_path):
    """Two lines for one name is a coin toss over which one wins."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"A=1\n{QA_EXECUTOR_ENV}=codex\nB=2\n", encoding="utf-8")

    write_qa_executor(env_file, "claude")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"{QA_EXECUTOR_ENV}=claude") == 1
    assert not [line for line in lines if line == f"{QA_EXECUTOR_ENV}=codex"]
    assert "A=1" in lines and "B=2" in lines


def test_env_values_keep_their_own_equals_signs(tmp_path):
    """Secrets carry '=' — a naive split truncates them into something invalid."""
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nKEY=abc=def==\n", encoding="utf-8")

    assert read_env_file(env_file) == {"KEY": "abc=def=="}


def test_the_matrix_covers_every_agent_against_every_other():
    assert set(SUITES["matrix"].combinations) == {
        (qa, worker) for qa in AGENTS for worker in AGENTS
    }
    assert len(SUITES["matrix"].combinations) == 4


def test_canonical_suites_have_exact_targets_and_timeouts():
    expected_targets = {
        "mega-noop": "tests/live/test_full_pipeline.py::TestFullPipeline",
        "mega-llm": "tests/live/test_full_pipeline.py::TestFullPipelineLLM",
        "mega-brief": "tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline",
        "mega-brief-package": (
            "tests/live/test_product_brief_package_pipeline.py::TestProductBriefPackagePipeline"
        ),
        "matrix": "tests/live/test_full_pipeline.py::TestFullPipelineLLM",
    }

    assert set(SUITES) == set(expected_targets)
    for name, target in expected_targets.items():
        assert SUITES[name].target == target
        assert SUITES[name].timeout_seconds > 0


def test_noop_cap_covers_both_stories_and_the_undeploy_lifecycle():
    """The cap covers the lifecycle, and the ledger is the waits it is made of.

    The ledger is in `shared/stand_deadlines.py` and the cap is checked against
    it there, at import. What this asserts is what that check cannot: that the
    ledger's entries are the *live* bounds the harness waits on rather than
    copies of them, and that the runner spends the derived cap.

    Round 5 of card 1316 is why the identity is asserted entry by entry. It
    booked 420 s for the merged deploy Run while `tests/live` had been waiting
    `DEPLOY_RUN_TIMEOUT` — 1320 s, the old 420 plus the producer's 900 s image
    bound — so the lifecycle understated itself by 1800 s while a cap *derived*
    from the ledger read as checked. A number that has to be remembered is not a
    ledger; a number that is the constant cannot drift from it.
    """
    first = dict(stand_deadlines.NOOP_FIRST_STORY_WAITS)
    second = dict(stand_deadlines.NOOP_SECOND_STORY_WAITS)
    teardown = dict(stand_deadlines.NOOP_TEARDOWN_WAITS)

    # Every entry is the constant the wait is made from, not a transcription.
    assert first["scaffold"] == stand_deadlines.scaffold_budget_seconds(
        stand_deadlines.NOOP_MODULE_COUNT
    )
    assert first["two ordered noop engineering Tasks"] == 2 * stand_deadlines.ENGINEERING_TIMEOUT
    assert (
        first["merged deploy Run, including the product's own image publication"]
        == stand_deadlines.DEPLOY_RUN_TIMEOUT
        == second["extension story: merged deploy Run, including image publication"]
    )
    assert first["deploy"] == stand_deadlines.DEPLOY_TIMEOUT
    assert first["typed deploy outcome"] == stand_deadlines.DEPLOY_OUTCOME_TIMEOUT
    assert first["public health probe"] == stand_deadlines.health_probe_budget_seconds()
    assert first["deterministic QA"] == stand_deadlines.QA_RUN_TIMEOUT
    assert first["Story.completed"] == stand_deadlines.STORY_COMPLETION_TIMEOUT
    assert first["durable PO completion notification"] == stand_deadlines.OWNER_NOTIFICATION_TIMEOUT
    assert first["exact service-deployment record"] == stand_deadlines.DEPLOY_OUTCOME_TIMEOUT
    assert (
        first["Story aggregation after both Tasks are done"]
        == stand_deadlines.STORY_AGGREGATION_TIMEOUT
    )
    assert second["extension story: one noop engineering Task"] == (
        stand_deadlines.ENGINEERING_TIMEOUT
    )
    assert second["extension story: typed deploy outcome, covering the deploy itself"] == (
        stand_deadlines.SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT
    )
    assert second["extension story: the application's own terminal status"] == (
        stand_deadlines.DEPLOY_TIMEOUT
    )
    assert set(teardown.values()) == {stand_deadlines.UNDEPLOY_TIMEOUT}

    # The totals the README states, and the cap the runner actually spends.
    assert sum(first.values()) == 4100
    assert sum(second.values()) == 3740
    assert stand_deadlines.noop_lifecycle_explicit_waits() == 8440
    assert NOOP_SUITE_TIMEOUT_SECONDS == 9300
    assert SUITES["mega-noop"].timeout_seconds == NOOP_SUITE_TIMEOUT_SECONDS
    assert (
        NOOP_SUITE_TIMEOUT_SECONDS - stand_deadlines.noop_lifecycle_explicit_waits()
        >= stand_deadlines.NOOP_TEARDOWN_RESERVE_SECONDS
    )
    # And it fits the one job the workflow gives the whole run: 45m provisioning,
    # 10m pre-provisioning reserve, preflight, readiness, the executor switch,
    # the suite, the sweep and the job reserve (`stand-e2e.yml`, 360 minutes).
    noop_job_seconds = (
        stand_run.STAND_PROVISIONING_TIMEOUT_SECONDS
        + stand_run.STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
        + stand_run.PREFLIGHT_TIMEOUT_SECONDS
        + stand_run.READINESS_TIMEOUT_SECONDS
        + stand_run.EXECUTOR_SWITCH_TIMEOUT_SECONDS
        + NOOP_SUITE_TIMEOUT_SECONDS
        + stand_run.SWEEP_TIMEOUT_SECONDS
        + stand_run.STAND_JOB_RESERVE_SECONDS
    )
    assert noop_job_seconds <= stand_run.STAND_JOB_TIMEOUT_MINUTES * 60


def test_brief_runner_ledger_reserves_a_hard_stop_after_productive_work():
    """The paid fixture retains evidence and cleans up before runner timeout."""
    assert BRIEF_SUITE_TIMEOUT_SECONDS == 50 * 60
    assert BRIEF_HARD_STOP_SECONDS == 60 * 60
    assert stand_run.BRIEF_CLEANUP_GRACE_SECONDS == (
        BRIEF_HARD_STOP_SECONDS - BRIEF_SUITE_TIMEOUT_SECONDS
    )
    assert BRIEF_RUNNER_TIMEOUT_SECONDS == (
        stand_run.PREFLIGHT_TIMEOUT_SECONDS
        + stand_run.READINESS_TIMEOUT_SECONDS
        + stand_run.EXECUTOR_SWITCH_TIMEOUT_SECONDS
        + BRIEF_HARD_STOP_SECONDS
        + stand_run.SWEEP_TIMEOUT_SECONDS
    )
    end_to_end = (
        STAND_PROVISIONING_TIMEOUT_SECONDS
        + STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
        + BRIEF_RUNNER_TIMEOUT_SECONDS
    )
    assert end_to_end < STAND_JOB_TIMEOUT_MINUTES * 60
    assert STAND_JOB_TIMEOUT_MINUTES * 60 - end_to_end >= STAND_JOB_RESERVE_SECONDS


def test_mega_brief_has_a_50_minute_productive_deadline_and_a_separate_cleanup_grace():
    assert stand_run.BRIEF_SUITE_TIMEOUT_SECONDS == 50 * 60
    assert stand_run.BRIEF_HARD_STOP_SECONDS >= 60 * 60
    assert stand_run.BRIEF_CLEANUP_GRACE_SECONDS > 0
    assert stand_run.SUITES["mega-brief"].timeout_seconds == 50 * 60
    assert (
        stand_run.SUITES["mega-brief"].timeout_seconds
        + stand_run.SUITES["mega-brief"].cleanup_grace_seconds
        == stand_run.BRIEF_HARD_STOP_SECONDS
    )


def test_the_package_brief_variant_is_one_named_suite_with_its_own_budget():
    """The PO dispatches the package route by name, not by pytest target.

    Its window is its own: the variant pays for a kit install — obtain the kit,
    build the wheel, `kit add`, regenerate — before its own engineering starts,
    and spending the digest variant's ledger on that is how a paid run dies
    inside its own deadline.
    """
    suite = stand_run.SUITES["mega-brief-package"]

    assert suite.llm is True
    assert suite.combinations == ()
    assert suite.timeout_seconds == BRIEF_PACKAGE_SUITE_TIMEOUT_SECONDS == 65 * 60
    assert suite.timeout_seconds > SUITES["mega-brief"].timeout_seconds
    assert suite.cleanup_grace_seconds > 0
    assert suite.timeout_seconds + suite.cleanup_grace_seconds == BRIEF_PACKAGE_HARD_STOP_SECONDS
    assert BRIEF_PACKAGE_RUNNER_TIMEOUT_SECONDS == (
        stand_run.PREFLIGHT_TIMEOUT_SECONDS
        + stand_run.READINESS_TIMEOUT_SECONDS
        + stand_run.EXECUTOR_SWITCH_TIMEOUT_SECONDS
        + BRIEF_PACKAGE_HARD_STOP_SECONDS
        + stand_run.SWEEP_TIMEOUT_SECONDS
    )
    end_to_end = (
        STAND_PROVISIONING_TIMEOUT_SECONDS
        + STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
        + BRIEF_PACKAGE_RUNNER_TIMEOUT_SECONDS
    )
    assert STAND_JOB_TIMEOUT_MINUTES * 60 - end_to_end >= STAND_JOB_RESERVE_SECONDS


def test_every_named_suite_reaches_the_help_epilog():
    """`--help` is how the PO finds a suite it did not already know about."""
    epilog = "; ".join(f"{name} — {one.description}" for name, one in SUITES.items())

    for name in SUITES:
        assert f"{name} — " in epilog


def test_legacy_aliases_resolve_to_canonical_suite_names():
    assert SUITE_ALIASES == {"mega": "mega-noop", "llm": "mega-llm"}

    for alias, canonical_name in SUITE_ALIASES.items():
        resolved_name, suite = resolve_suite(alias)

        assert resolved_name == canonical_name
        assert suite is SUITES[canonical_name]


def test_unknown_suite_is_a_non_llm_pytest_target():
    name, suite = resolve_suite("tests/live/test_api_crud.py")

    assert name == "tests/live/test_api_crud.py"
    assert suite.target == "tests/live/test_api_crud.py"
    assert suite.llm is False


def test_mega_llm_runs_the_one_requested_agent_pair():
    assert SUITES["mega-llm"].combinations == ()


def test_mega_brief_runs_the_one_requested_agent_pair():
    """The Product Brief proof spends one developer and one QA executor pair."""
    assert SUITES["mega-brief"].llm is True
    assert SUITES["mega-brief"].combinations == ()


def test_named_suites_state_what_they_run():
    for name, suite in SUITES.items():
        assert suite.target, name
        assert suite.description, name


def test_an_unknown_suite_is_taken_as_a_pytest_target():
    """A suite the table does not name is a target, so a new e2e needs no code."""
    assert SUITES.get("tests/live/test_api_crud.py") is None

    suite = SUITES.get("tests/live/test_api_crud.py") or Suite(
        target="tests/live/test_api_crud.py", llm=False
    )

    assert suite.target == "tests/live/test_api_crud.py"
    assert suite.combinations == ()


@pytest.mark.parametrize("status", ["passed", "failed", "qa_executor_switch_failed"])
def test_every_outcome_is_one_report_row(status):
    assert matrix_row("matrix", "codex", "claude", status, 42) == (
        f"matrix\tcodex\tclaude\t{status}\t42\n"
    )


def test_junit_report_records_a_passing_suite(tmp_path):
    report = tmp_path / "junit.xml"

    write_junit_report(report, "mega-noop", [("codex", "claude", "passed", 42)])

    contents = report.read_text(encoding="utf-8")
    assert 'name="stand-e2e:mega-noop"' in contents
    assert 'tests="1"' in contents
    assert 'failures="0"' in contents
    assert 'name="qa=codex worker=claude"' in contents


def test_junit_report_records_a_failed_suite(tmp_path):
    report = tmp_path / "junit.xml"

    write_junit_report(report, "mega-llm", [("claude", "codex", "failed", 7)])

    contents = report.read_text(encoding="utf-8")
    assert 'failures="1"' in contents
    assert '<failure message="failed"' in contents


@pytest.mark.parametrize(("passed", "expected_exit", "failures"), [(True, 0, "0"), (False, 1, "1")])
def test_runner_writes_report_and_junit_for_success_and_failure(
    tmp_path, monkeypatch, passed, expected_exit, failures
):
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "sweep", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "run_pytest", lambda *_args: passed)
    monkeypatch.setattr(
        stand_run.sys,
        "argv",
        ["stand_run.py", "--suite", "mega", "--skip-sweep"],
    )

    assert stand_run.main() == expected_exit

    run_dir = next(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir() and not path.is_symlink()
    )
    expected_status = "passed" if passed else "failed"
    assert run_dir.name.startswith("mega-noop-")
    assert "mega-noop\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")
    assert f"\t{expected_status}\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")
    assert f'failures="{failures}"' in (run_dir / "junit.xml").read_text(encoding="utf-8")


def test_noop_runner_scrubs_llm_environment_and_passes_the_suite_timeout(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {"LIVE_LLM_QA": "1"})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "sweep", lambda _env, _log: True)

    def fake_run(target, env, extra, log_path, timeout_seconds, termination_grace_seconds, log):
        captured.update(
            target=target,
            env=env,
            extra=extra,
            timeout_seconds=timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log=log,
        )
        return True

    monkeypatch.setattr(stand_run, "run_pytest", fake_run)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop"])

    assert stand_run.main() == 0
    assert captured["target"] == SUITES["mega-noop"].target
    assert captured["extra"] == {}
    assert captured["timeout_seconds"] == SUITES["mega-noop"].timeout_seconds


def test_run_pytest_streams_redacted_console_output_and_preserves_the_log(
    tmp_path, monkeypatch, capsys
):
    captured: dict[str, object] = {}

    class Process:
        stdout = iter(
            [
                "stage=engineering token=protected\n",
                "heartbeat elapsed_seconds=30 " + "x" * 5000 + "\n",
            ]
        )
        returncode = 0
        pid = 123

        def wait(self, *, timeout=None):
            captured.setdefault("waits", []).append(timeout)
            return 0

        def send_signal(self, _signal):
            raise AssertionError("successful pytest must not be interrupted")

        def kill(self):
            raise AssertionError("successful pytest must not be killed")

    def fake_popen(_command, **kwargs):
        captured.update(kwargs)
        captured["command"] = _command
        return Process()

    monkeypatch.setattr(stand_run.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("LIVE_LLM_QA", "stale")
    monkeypatch.setenv("LIVE_QA_AGENT_TYPE", "stale")
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", "stale")

    assert (
        stand_run.run_pytest(
            "test_target", {"API_TOKEN": "protected"}, {}, tmp_path / "pytest.log", 123
        )
        is stand_run.PytestOutcome.PASSED
    )
    assert captured["waits"] == [123]
    assert "-s" in captured["command"]
    assert captured["start_new_session"] is True
    assert captured["env"][stand_run.LIVE_EVIDENCE_OUTPUT_DIR_ENV] == str(tmp_path)
    for name in stand_run.LLM_ENV_NAMES:
        assert name not in captured["env"]

    log = (tmp_path / "pytest.log").read_text(encoding="utf-8")
    assert "protected" not in log
    assert "[redacted]" in log
    assert len(log.splitlines()[1]) <= stand_run.LIVE_RELAY_LINE_MAX_CHARS
    assert "stage=engineering" in capsys.readouterr().out

    assert (
        stand_run.run_pytest(
            "test_target",
            {},
            {"LIVE_LLM_QA": "1", "LIVE_QA_AGENT_TYPE": "codex", "LIVE_WORKER_AGENT_TYPE": "claude"},
            tmp_path / "pytest-llm.log",
            456,
        )
        is stand_run.PytestOutcome.PASSED
    )
    assert captured["env"]["LIVE_LLM_QA"] == "1"
    assert captured["env"]["LIVE_QA_AGENT_TYPE"] == "codex"
    assert captured["env"]["LIVE_WORKER_AGENT_TYPE"] == "claude"


def test_run_pytest_interrupts_and_kills_the_process_group_after_its_grace(tmp_path, monkeypatch):
    captured: list[object] = []
    messages: list[str] = []

    class Process:
        stdout = iter(())
        returncode = 1
        pid = 456

        def wait(self, *, timeout=None):
            captured.append(("wait", timeout))
            if timeout in {50 * 60, 300}:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return 1

    monkeypatch.setattr(stand_run.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        stand_run.os, "killpg", lambda pid, signal: captured.append(("group_signal", pid, signal))
    )

    assert (
        stand_run.run_pytest(
            "target", {}, {}, tmp_path / "timeout.log", 50 * 60, 300, messages.append
        )
        is stand_run.PytestOutcome.TIMED_OUT
    )
    assert captured == [
        ("wait", 50 * 60),
        ("group_signal", 456, stand_run.signal.SIGINT),
        ("wait", 300),
        ("group_signal", 456, stand_run.signal.SIGKILL),
        ("wait", None),
    ]
    assert "pytest hard deadline exhausted" in messages[0]
    assert "process-group termination grace exhausted" in messages[1]


def test_run_pytest_does_not_deadlock_on_a_wedged_grandchild_output_pipe(tmp_path, monkeypatch):
    calls: list[object] = []

    class Pipe:
        def __iter__(self):
            return iter(())

        def close(self):
            calls.append("pipe_closed")

    class Process:
        stdout = Pipe()
        returncode = 0
        pid = 789

        def wait(self, *, timeout=None):
            return 0

    class StuckThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, *, timeout=None):
            calls.append(("join", timeout))

        def is_alive(self):
            return True

    monkeypatch.setattr(stand_run.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(stand_run, "Thread", StuckThread)

    assert (
        stand_run.run_pytest(
            "target", {}, {}, tmp_path / "stuck.log", 1, log=lambda line: calls.append(line)
        )
        is stand_run.PytestOutcome.PASSED
    )
    assert calls == [
        ("join", stand_run.RELAY_JOIN_TIMEOUT_SECONDS),
        f"pytest output relay remained open after {stand_run.RELAY_JOIN_TIMEOUT_SECONDS}s",
        "pipe_closed",
        ("join", stand_run.RELAY_JOIN_TIMEOUT_SECONDS),
    ]


def test_preflight_failure_is_red_and_records_the_canonical_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: False)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega"])

    assert stand_run.main() == 2
    run_dir = next(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
    assert "mega-noop\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")


def test_pytest_timeout_is_distinct_in_the_report_and_cleanup_failure_is_red(tmp_path, monkeypatch):
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "sweep", lambda _env, _log: False)

    monkeypatch.setattr(stand_run, "run_pytest", lambda *_args: stand_run.PytestOutcome.TIMED_OUT)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop"])

    assert stand_run.main() == 1
    report = next((tmp_path / "runs").glob("*/report.tsv")).read_text(encoding="utf-8")
    assert "timed_out" in report


def test_the_recreate_set_is_derived_from_compose_and_names_the_deciding_service():
    """`api` resolves the QA executor, so a switch that skips it changes nothing.

    Run 33743251165 asked for `claude`, recreated `qa-worker` alone, and ran QA on
    Codex: the `api` container still held the value it had started with, and the
    resolver reads it from there.
    """
    services = qa_executor_services()

    assert "api" in services
    assert "qa-worker" in services
    assert services == tuple(sorted(services))


def test_a_new_service_that_reads_the_variable_needs_no_edit_here(tmp_path):
    """The derivation is the point: a transcribed list is how this defect survived."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    environment:\n"
        f"      {QA_EXECUTOR_ENV}: ${{{QA_EXECUTOR_ENV}:-codex}}\n"
        "  qa-worker:\n"
        "    environment:\n"
        f"      - {QA_EXECUTOR_ENV}=codex\n"
        "  scheduler:\n"
        "    environment:\n"
        "      SERVICE_NAME: scheduler\n",
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.prod.yml").write_text(
        "services:\n"
        "  future-consumer:\n"
        "    ports: !reset []\n"
        "    environment:\n"
        f"      {QA_EXECUTOR_ENV}: ${{{QA_EXECUTOR_ENV}:-codex}}\n",
        encoding="utf-8",
    )

    assert qa_executor_services(tmp_path) == ("api", "future-consumer", "qa-worker")


def test_a_service_whose_environment_cannot_be_read_is_refused_not_skipped(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    environment:\n"
        f"      {QA_EXECUTOR_ENV}: codex\n"
        "  clone:\n"
        "    extends:\n"
        "      service: api\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="extends"):
        qa_executor_services(tmp_path)


def test_no_service_reading_the_variable_is_a_refusal(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  api:\n    environment:\n      SERVICE_NAME: api\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match=QA_EXECUTOR_ENV):
        qa_executor_services(tmp_path)


class _ComposeStand:
    """A compose stack whose services each hold their own copy of the value.

    It also models the thing run 33749154999 exposed: a container answers
    `docker compose exec` well before it serves the port the suite calls, so
    `serves_health` and the consumer's startup line lag the recreate by a
    configurable number of probes.
    """

    def __init__(
        self,
        *,
        initial: str,
        flips: tuple[str, ...],
        http_ready_after: int = 0,
        started_after: int = 0,
    ):
        #: The services a recreate is allowed to change. `flips` is how the old
        #: defect is reproduced: recreate qa-worker, leave api where it was.
        self.values = dict.fromkeys(qa_executor_services(), initial)
        self.flips = flips
        self.recreated: list[str] = []
        self.env_path = None
        #: How many health probes fail before uvicorn listens, and how many log
        #: reads come back empty before the consumer announces it started.
        self.http_ready_after = http_ready_after
        self.started_after = started_after
        self.health_probes = 0
        self.log_reads: dict[str, int] = {}
        #: Everything the runner did, in order: this is what proves the wait
        #: happens before the decision and the decision before pytest.
        self.events: list[str] = []

    def serves_health(self) -> bool:
        self.health_probes += 1
        self.events.append("health")
        return self.health_probes > self.http_ready_after

    def __call__(self, env, *args, capture=False):
        if args[0] == "up":
            recreated = [name for name in args if name in self.values]
            self.recreated.extend(recreated)
            self.events.append("up")
            for name in recreated:
                if name in self.flips:
                    self.values[name] = read_env_file(self.env_path)[QA_EXECUTOR_ENV]
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        if args[0] == "logs":
            service = args[-1]
            self.events.append(f"logs:{service}")
            self.log_reads[service] = self.log_reads.get(service, 0) + 1
            started = self.log_reads[service] > self.started_after
            body = f'{{"event": "{service}_started"}}\n' if started else ""
            return subprocess.CompletedProcess([], 0, stdout=body, stderr="")
        assert args[0] == "exec"
        service = args[2]
        self.events.append("resolve")
        return subprocess.CompletedProcess([], 0, stdout=f"{self.values[service]}\n", stderr="")


def _stand_env(tmp_path, monkeypatch, stand):
    env_path = tmp_path / ".env"
    env_path.write_text(f"{QA_EXECUTOR_ENV}=codex\n", encoding="utf-8")
    stand.env_path = env_path
    monkeypatch.setattr(stand_run, "REPO", tmp_path)
    monkeypatch.setattr(stand_run, "_compose", stand)
    monkeypatch.setattr(stand_run, "api_serves_health", stand.serves_health)
    monkeypatch.setattr(stand_run, "EXECUTOR_SWITCH_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(stand_run.time, "sleep", lambda _seconds: None)
    return env_path


def test_the_switch_is_confirmed_by_the_resolver_and_recreates_every_reader(tmp_path, monkeypatch):
    stand = _ComposeStand(initial="codex", flips=qa_executor_services())
    _stand_env(tmp_path, monkeypatch, stand)
    lines: list[str] = []

    assert stand_run.ensure_qa_executor({}, "claude", lines.append) is True
    assert sorted(stand.recreated) == sorted(qa_executor_services())
    assert lines == []


def test_a_consumer_that_flipped_alone_does_not_satisfy_the_switch(tmp_path, monkeypatch):
    """The defect itself: qa-worker reports `claude` while `api` still resolves codex."""
    stand = _ComposeStand(initial="codex", flips=("qa-worker",))
    _stand_env(tmp_path, monkeypatch, stand)
    lines: list[str] = []

    assert stand_run.ensure_qa_executor({}, "claude", lines.append) is False
    assert stand.values["qa-worker"] == "claude"
    assert lines and "never answered 'claude'" in lines[0]


def test_the_confirmation_asks_the_api_for_the_resolver_s_own_decision(monkeypatch):
    """Not the qa-worker's local settings, which only echo the recreate just done."""
    calls: list[tuple] = []

    def fake_compose(env, *args, capture=False):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout="claude\n", stderr="")

    monkeypatch.setattr(stand_run, "_compose", fake_compose)

    assert stand_run.resolved_qa_executor({}) == "claude"
    assert calls[0][:4] == ("exec", "-T", "api", "python")
    snippet = calls[0][5]
    assert "resolve_executor_decision" in snippet
    assert "RunType.QA" in snippet
    assert "qa_executor_agent_type" not in snippet


def test_bringing_a_service_up_outside_the_gate_is_refused(monkeypatch):
    """Recreating and waiting are one operation, enforced rather than remembered.

    1257 widened the recreate set correctly and 33749154999 still died, because a
    caller could bring a container up and walk straight into pytest. A lifecycle
    verb outside `recreate_and_wait` is now an error, so the next caller inherits
    the wait instead of having to remember it.
    """
    ran: list[list[str]] = []
    monkeypatch.setattr(
        stand_run.subprocess,
        "run",
        lambda command, **_kwargs: (
            ran.append(command) or subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ),
    )

    for verb in stand_run.COMPOSE_LIFECYCLE_COMMANDS:
        with pytest.raises(RuntimeError, match="recreate_and_wait"):
            stand_run._compose({}, verb, "-d", "api")

    assert ran == []
    # The same call inside the gate is the one legitimate way through.
    with stand_run._recreate_gate():
        stand_run._compose({}, "up", "-d", "--force-recreate", "api")
    assert ran and ran[0][-3:] == ["-d", "--force-recreate", "api"]


def test_readiness_asks_for_health_over_http_the_way_the_suite_does(monkeypatch):
    """An in-container probe passed on 33749154999 while the suite could not connect."""
    asked: list[tuple[str, object]] = []

    class _Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(url, timeout=None):
        asked.append((url, timeout))
        if len(asked) == 1:
            raise OSError("connection refused")
        return _Response(200)

    monkeypatch.setattr(stand_run.urllib.request, "urlopen", fake_urlopen)

    assert stand_run.api_serves_health() is False
    assert stand_run.api_serves_health() is True
    assert asked[0][0] == "http://localhost:8000/health"
    assert asked[0][1] == stand_run.READINESS_POLL_SECONDS


def test_the_probe_uses_the_base_url_the_live_suite_builds_its_clients_on():
    """One URL, read from the suite: a probe of a different host proves nothing."""
    conftest = (stand_run.REPO / "tests" / "live" / "conftest.py").read_text(encoding="utf-8")

    assert f'API_URL = "{stand_run.SUITE_API_BASE_URL}"' in conftest


def test_a_consumer_is_ready_only_once_it_says_it_started(tmp_path, monkeypatch):
    """`qa-worker` running is not `qa-worker` consuming its queue."""
    stand = _ComposeStand(initial="codex", flips=qa_executor_services(), started_after=1)
    _stand_env(tmp_path, monkeypatch, stand)

    assert stand_run.service_is_ready({}, "qa-worker") is False
    assert stand_run.service_is_ready({}, "qa-worker") is True


def test_the_runner_waits_for_http_while_the_resolver_already_answers(tmp_path, monkeypatch):
    """The discriminating case of run 33749154999.

    The recreated `api` can import the resolver — and therefore answer the
    decision — seconds before uvicorn listens. The runner must not take that
    answer as permission to start pytest.
    """
    stand = _ComposeStand(initial="codex", flips=qa_executor_services(), http_ready_after=3)
    _stand_env(tmp_path, monkeypatch, stand)
    lines: list[str] = []

    assert stand_run.ensure_qa_executor({}, "claude", lines.append) is True

    after_recreate = stand.events[stand.events.index("up") :]
    assert after_recreate.count("health") == 4
    # Readiness first, the resolver's decision after it, and no decision taken
    # from the window in which the suite could not have connected.
    assert after_recreate.index("logs:qa-worker") > _last_index(after_recreate, "health")
    assert _last_index(after_recreate, "resolve") > _last_index(after_recreate, "health")
    assert lines == []


def test_a_readiness_timeout_refuses_the_cell_instead_of_starting_pytest(tmp_path, monkeypatch):
    stand = _ComposeStand(initial="codex", flips=qa_executor_services(), http_ready_after=1_000)
    _stand_env(tmp_path, monkeypatch, stand)
    monkeypatch.setattr(stand_run, "READINESS_TIMEOUT_SECONDS", 0)
    lines: list[str] = []

    assert stand_run.ensure_qa_executor({}, "claude", lines.append) is False
    assert stand.recreated  # it did recreate; it simply refused to proceed
    assert "resolve" not in stand.events[stand.events.index("up") :]
    assert lines and "api was not usable" in lines[0]


def test_an_unready_stack_is_reported_as_a_failed_switch_and_skips_the_cell(tmp_path, monkeypatch):
    """A switch that never became usable ends the cell the way 1257 ends one."""
    started: list[str] = []
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "sweep", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "ensure_qa_executor", lambda _env, _qa, _log: False)
    monkeypatch.setattr(stand_run, "run_pytest", lambda *args: started.append(args[0]) or True)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-llm"])

    assert stand_run.main() == 1
    assert started == []
    run_dir = next(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir() and not path.is_symlink()
    )
    assert "\tqa_executor_switch_failed\t0\n" in (run_dir / "report.tsv").read_text(
        encoding="utf-8"
    )


def _last_index(events: list[str], name: str) -> int:
    return len(events) - 1 - events[::-1].index(name)


# --- the stand runs the pulled service release, never a build ----------------------------
#
# Bring-up no longer leaves a `codegen-orchestrator/*:local` image on the stand, so a
# recreate without the release override finds no image and builds the service from
# the checkout. These run the real `_compose` against a fake `docker` on PATH.

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG}"
case "$*" in
    *" logs "*) echo "{\\"event\\": \\"${@: -1}_started\\"}" ;;
esac
exit 0
"""


def _fake_docker(tmp_path, monkeypatch):
    """A stand checkout with its generated override, and a docker that records calls."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "deployed-service-images.compose.yml").write_text(
        "services:\n  qa-worker:\n    image: ghcr.io/o/qa-worker@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(stand_run, "REPO", checkout)
    # Relative, the way the workflow names it: the file bring-up wrote in the checkout.
    monkeypatch.setenv(
        stand_run.SERVICE_RELEASE_OVERRIDE_ENV, "deployed-service-images.compose.yml"
    )
    monkeypatch.setattr(stand_run.time, "sleep", lambda _seconds: None)
    env = {"PATH": f"{binaries}:/usr/bin:/bin", "FAKE_DOCKER_LOG": str(log)}
    return env, log, checkout


def test_the_runner_recreates_from_the_pulled_release_and_builds_nothing(tmp_path, monkeypatch):
    env, log, checkout = _fake_docker(tmp_path, monkeypatch)
    lines: list[str] = []

    assert stand_run.recreate_and_wait(env, ("qa-worker",), lines.append) is True

    calls = log.read_text(encoding="utf-8").splitlines()
    assert lines == []
    files = (
        "compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.stand.yml "
        f"-f {checkout / 'deployed-service-images.compose.yml'} "
    )
    assert calls[0] == (
        files + "up --no-build --pull never -d --no-deps --force-recreate qa-worker"
    )
    assert calls[1:] and all(call.startswith(files) for call in calls[1:])
    for call in calls:
        assert " build" not in call and "--build " not in call


@pytest.mark.parametrize("named", ["", "deployed-service-images.compose.yml"])
def test_a_recreate_without_the_release_override_is_refused_before_compose_runs(
    tmp_path, monkeypatch, named
):
    """Unset, or naming a file bring-up never wrote: refused, never built instead."""
    env, log, checkout = _fake_docker(tmp_path, monkeypatch)
    (checkout / "deployed-service-images.compose.yml").unlink()
    if named:
        monkeypatch.setenv(stand_run.SERVICE_RELEASE_OVERRIDE_ENV, named)
    else:
        monkeypatch.delenv(stand_run.SERVICE_RELEASE_OVERRIDE_ENV)

    with pytest.raises(stand_run.ReleaseOverrideMissing, match="build the services"):
        stand_run.recreate_and_wait(env, ("qa-worker",), print)
    with pytest.raises(stand_run.ReleaseOverrideMissing):
        stand_run.resolved_qa_executor(env)

    assert log.read_text(encoding="utf-8") == ""


def test_a_run_without_the_release_override_is_refused_before_anything_is_spent(
    tmp_path, monkeypatch
):
    started: list[str] = []
    monkeypatch.delenv(stand_run.SERVICE_RELEASE_OVERRIDE_ENV)
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: started.append("preflight"))
    monkeypatch.setattr(stand_run, "ensure_qa_executor", lambda *_args: started.append("switch"))
    monkeypatch.setattr(stand_run, "run_pytest", lambda *args: started.append("pytest"))
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "matrix"])

    assert stand_run.main() == 2
    assert started == []
    report = next((tmp_path / "runs").glob("*/report.tsv")).read_text(encoding="utf-8")
    assert "\trelease_override_missing\t0\n" in report


@pytest.mark.parametrize("verb", stand_run.COMPOSE_REFUSED_COMMANDS)
def test_no_compose_verb_that_builds_pulls_or_skips_the_policy_gets_through(
    tmp_path, monkeypatch, verb
):
    """`start` and `restart` take no --no-build, `run` no --no-build either: the
    runner brings containers up only with the one `up` that carries the policy."""
    env, log, _checkout = _fake_docker(tmp_path, monkeypatch)

    with stand_run._recreate_gate(), pytest.raises(RuntimeError, match="build or pull"):
        stand_run._compose(env, verb, "api")
    with stand_run._recreate_gate(), pytest.raises(RuntimeError, match="build or pull"):
        stand_run._compose(env, "up", "-d", "--build", "api")

    assert log.read_text(encoding="utf-8") == ""


def test_every_compose_call_of_the_runner_goes_through_the_one_policed_door():
    """A second `docker compose` call site would not carry the override or the policy."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(stand_run))
    owners: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and node.value == "compose":
                owners.append(function.name)

    assert owners == ["_compose"]


FAKE_UV = """#!/bin/bash
printf '%s\\n' "$*" > "$FAKE_UV_ARGS"
env > "$FAKE_UV_ENV"
exit "${FAKE_UV_EXIT:-0}"
"""


def _fake_uv(tmp_path, *, exit_code=0):
    """A `uv` that records how the sweep was started and with what environment."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    args, env_dump = tmp_path / "uv.args", tmp_path / "uv.env"
    env = {
        "PATH": f"{binaries}:/usr/bin:/bin",
        "FAKE_UV_ARGS": str(args),
        "FAKE_UV_ENV": str(env_dump),
        "FAKE_UV_EXIT": str(exit_code),
    }

    def recorded() -> tuple[str, dict[str, str]]:
        lines = env_dump.read_text(encoding="utf-8").splitlines()
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        return args.read_text(encoding="utf-8").strip(), values

    return env, recorded


@pytest.mark.parametrize("exported", [None, "http://api:8000", "https://elsewhere.example"])
def test_the_sweep_addresses_the_api_the_suites_used(tmp_path, monkeypatch, exported):
    """Run 35945831487: 39 passed, then `API_BASE_URL is required` made the run red.

    Neither an exported value nor the deployed `.env` (whose `http://api:8000` is
    the container network's name) may point the sweep at another API than the
    suites' clients.
    """
    env, recorded = _fake_uv(tmp_path)
    if exported is not None:
        monkeypatch.setenv(clean_live_tests.API_BASE_URL_ENV, exported)
        env[clean_live_tests.API_BASE_URL_ENV] = exported
    lines: list[str] = []

    assert stand_run.sweep(env, lines.append) is True

    argv, sweep_env = recorded()
    assert argv == "run python -m scripts.clean_live_tests"
    assert sweep_env[clean_live_tests.API_BASE_URL_ENV] == stand_run.SUITE_API_BASE_URL
    assert sweep_env["LIVE_CONTOUR"] == "stand"
    assert sweep_env[clean_live_tests.INTERNAL_API_KEY_ENV] == "test-internal-key"
    assert lines == []


def test_a_failed_sweep_is_red_and_names_its_last_line(tmp_path):
    env, _recorded = _fake_uv(tmp_path, exit_code=1)
    lines: list[str] = []

    assert stand_run.sweep(env, lines.append) is False
    assert lines and lines[0].startswith("sweep failed:")


def test_the_suite_and_the_sweep_are_given_the_same_contour():
    assert (
        stand_run.sweep_environment({})["LIVE_CONTOUR"] == stand_run.STAND_CONTOUR.name == "stand"
    )


def _count_spending(monkeypatch) -> list[str]:
    started: list[str] = []
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: started.append("preflight"))
    monkeypatch.setattr(stand_run, "ensure_qa_executor", lambda *_args: started.append("switch"))
    monkeypatch.setattr(stand_run, "run_pytest", lambda *args: started.append("pytest"))
    monkeypatch.setattr(stand_run, "sweep", lambda *_args: started.append("sweep"))
    return started


@pytest.mark.parametrize(
    "missing",
    [
        name
        for name in clean_live_tests.sweep_requirements(stand_run.STAND_CONTOUR)
        if name != clean_live_tests.API_BASE_URL_ENV
    ],
)
def test_a_run_whose_sweep_could_not_start_is_refused_before_anything_is_spent(
    tmp_path, monkeypatch, missing
):
    started = _count_spending(monkeypatch)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {missing: "  "})
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop"])

    assert stand_run.main() == 2
    assert started == []
    run_dir = next(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
    assert "mega-noop\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")
    assert "\tsweep_requirements_missing\t0\n" in (run_dir / "report.tsv").read_text(
        encoding="utf-8"
    )
    assert f"sweep cannot run without {missing}" in (run_dir / "run.log").read_text(
        encoding="utf-8"
    )
    assert 'failures="1"' in (run_dir / "junit.xml").read_text(encoding="utf-8")


def test_the_deployed_env_satisfies_the_sweep_the_way_it_configures_it(tmp_path, monkeypatch):
    """On the stand the key and the run tag come from `.env`, not the shell."""
    for name in (clean_live_tests.INTERNAL_API_KEY_ENV, clean_live_tests.STAND_RUN_TAG_ENV):
        monkeypatch.delenv(name)
    deployed = {
        clean_live_tests.INTERNAL_API_KEY_ENV: "k",
        clean_live_tests.STAND_RUN_TAG_ENV: "gha-1-1",
    }

    assert stand_run.sweep_requirements_refusal(deployed, print) is None
    assert stand_run.sweep_requirements_refusal({}, print) == "sweep_requirements_missing"


def test_the_entry_check_is_the_sweep_s_own_list_and_cannot_drift(monkeypatch):
    """The runner holds no copy: a requirement the sweep adds is refused at entry.

    And what the entry check reads is the environment `sweep` passes, so the one
    variable the runner forms itself is satisfied by that and by nothing else.
    """
    lines: list[str] = []
    original = clean_live_tests.sweep_requirements
    monkeypatch.setattr(
        clean_live_tests, "sweep_requirements", lambda contour: (*original(contour), "NEW_NEED")
    )

    assert stand_run.sweep_requirements_refusal({}, lines.append) == "sweep_requirements_missing"
    assert lines == ["refused: the post-suite sweep cannot run without NEW_NEED"]
    assert stand_run.sweep_requirements_refusal({"NEW_NEED": "1"}, lines.append) is None


def test_a_run_that_skips_the_sweep_does_not_need_its_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv(clean_live_tests.INTERNAL_API_KEY_ENV)
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "run_pytest", lambda *_args: True)
    monkeypatch.setattr(
        stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop", "--skip-sweep"]
    )

    assert stand_run.main() == 0


@pytest.mark.parametrize(("configured", "expected"), [(True, 0), (False, 2)])
def test_stand_clean_sweeps_through_the_runner_s_check(tmp_path, monkeypatch, configured, expected):
    started = _count_spending(monkeypatch)
    monkeypatch.setattr(stand_run, "sweep", lambda *_args: started.append("sweep") or True)
    if not configured:
        monkeypatch.delenv(clean_live_tests.STAND_RUN_TAG_ENV)
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--sweep-only"])

    assert stand_run.main() == expected
    assert started == (["sweep"] if configured else [])
    assert not (tmp_path / "runs").exists()


def test_make_stand_clean_is_the_runner_s_sweep():
    makefile = (stand_run.REPO / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\nstand-clean:\n", 1)[1].split("\n\n", 1)[0]

    assert target.strip() == "@uv run python -m scripts.stand_run --sweep-only"
