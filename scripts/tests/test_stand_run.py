"""What the stand runner must not forget — each of these cost a run to learn."""

import pytest

from scripts import stand_run
from scripts.stand_run import (
    AGENTS,
    NOOP_SUITE_TIMEOUT_SECONDS,
    QA_EXECUTOR_ENV,
    SUITE_ALIASES,
    SUITES,
    Suite,
    compose_environment,
    matrix_row,
    read_env_file,
    resolve_suite,
    write_junit_report,
    write_qa_executor,
)


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
        "matrix": "tests/live/test_full_pipeline.py::TestFullPipelineLLM",
    }

    assert set(SUITES) == set(expected_targets)
    for name, target in expected_targets.items():
        assert SUITES[name].target == target
        assert SUITES[name].timeout_seconds > 0


def test_noop_cap_covers_the_completed_story_and_undeploy_lifecycle():
    """A new lifecycle wait must not silently exceed the named suite cap."""
    explicit_waits = (
        120  # scaffold
        + 840  # two ordered noop engineering Tasks
        + 60  # story aggregation after both Tasks are done
        + 420  # merged deploy Run
        + 420  # deploy
        + 120  # typed deploy outcome
        + 320  # five-attempt public health probe (two 30s paths + four sleeps)
        + 300  # deterministic QA
        + 180  # Story.completed
        + 180  # durable PO notification
        + 120  # exact service-deployment record
        + 300  # undeploy Run
        + 300  # terminal application/resource release
    )

    assert explicit_waits == 3680
    assert NOOP_SUITE_TIMEOUT_SECONDS == 4500
    assert NOOP_SUITE_TIMEOUT_SECONDS - explicit_waits >= 800


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

    def fake_run(target, env, extra, log_path, timeout_seconds):
        captured.update(target=target, env=env, extra=extra, timeout_seconds=timeout_seconds)
        return True

    monkeypatch.setattr(stand_run, "run_pytest", fake_run)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop"])

    assert stand_run.main() == 0
    assert captured["target"] == SUITES["mega-noop"].target
    assert captured["extra"] == {}
    assert captured["timeout_seconds"] == SUITES["mega-noop"].timeout_seconds


def test_run_pytest_strips_llm_environment_unless_the_suite_explicitly_sets_it(
    tmp_path, monkeypatch
):
    captured: dict[str, object] = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs)
        return __import__("subprocess").CompletedProcess([], 0)

    monkeypatch.setattr(stand_run.subprocess, "run", fake_run)
    monkeypatch.setenv("LIVE_LLM_QA", "stale")
    monkeypatch.setenv("LIVE_QA_AGENT_TYPE", "stale")
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", "stale")

    assert stand_run.run_pytest("test_target", {}, {}, tmp_path / "pytest.log", 123) is True
    assert captured["timeout"] == 123
    for name in stand_run.LLM_ENV_NAMES:
        assert name not in captured["env"]

    assert (
        stand_run.run_pytest(
            "test_target",
            {},
            {"LIVE_LLM_QA": "1", "LIVE_QA_AGENT_TYPE": "codex", "LIVE_WORKER_AGENT_TYPE": "claude"},
            tmp_path / "pytest-llm.log",
            456,
        )
        is True
    )
    assert captured["timeout"] == 456
    assert captured["env"]["LIVE_LLM_QA"] == "1"
    assert captured["env"]["LIVE_QA_AGENT_TYPE"] == "codex"
    assert captured["env"]["LIVE_WORKER_AGENT_TYPE"] == "claude"


def test_preflight_failure_is_red_and_records_the_canonical_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: False)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega"])

    assert stand_run.main() == 2
    run_dir = next(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
    assert "mega-noop\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")


def test_pytest_timeout_and_cleanup_failure_are_red(tmp_path, monkeypatch):
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: {})
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "sweep", lambda _env, _log: False)

    def timeout(*_args):
        raise __import__("subprocess").TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(stand_run, "run_pytest", timeout)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", "--suite", "mega-noop"])

    assert stand_run.main() == 1
