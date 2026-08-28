"""What the stand runner must not forget — each of these cost a run to learn."""

import pytest

from scripts import stand_run
from scripts.stand_run import (
    AGENTS,
    QA_EXECUTOR_ENV,
    SUITES,
    Suite,
    compose_environment,
    matrix_row,
    read_env_file,
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
    assert matrix_row("codex", "claude", status, 42) == f"codex\tclaude\t{status}\t42\n"


def test_junit_report_records_a_passing_suite(tmp_path):
    report = tmp_path / "junit.xml"

    write_junit_report(report, [("codex", "claude", "passed", 42)])

    contents = report.read_text(encoding="utf-8")
    assert 'tests="1"' in contents
    assert 'failures="0"' in contents
    assert 'name="qa=codex worker=claude"' in contents


def test_junit_report_records_a_failed_suite(tmp_path):
    report = tmp_path / "junit.xml"

    write_junit_report(report, [("claude", "codex", "failed", 7)])

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
    assert f"\t{expected_status}\t" in (run_dir / "report.tsv").read_text(encoding="utf-8")
    assert f'failures="{failures}"' in (run_dir / "junit.xml").read_text(encoding="utf-8")
