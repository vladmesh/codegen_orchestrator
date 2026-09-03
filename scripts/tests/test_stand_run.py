"""What the stand runner must not forget — each of these cost a run to learn."""

import subprocess

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
    qa_executor_services,
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
    assert captured["env"][stand_run.LIVE_EVIDENCE_OUTPUT_DIR_ENV] == str(tmp_path)
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
    """A compose stack whose services each hold their own copy of the value."""

    def __init__(self, *, initial: str, flips: tuple[str, ...]):
        #: The services a recreate is allowed to change. `flips` is how the old
        #: defect is reproduced: recreate qa-worker, leave api where it was.
        self.values = dict.fromkeys(qa_executor_services(), initial)
        self.flips = flips
        self.recreated: list[str] = []
        self.env_path = None

    def __call__(self, env, *args, capture=False):
        if args[0] == "up":
            recreated = [name for name in args if name in self.values]
            self.recreated.extend(recreated)
            for name in recreated:
                if name in self.flips:
                    self.values[name] = read_env_file(self.env_path)[QA_EXECUTOR_ENV]
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        assert args[0] == "exec"
        service = args[2]
        return subprocess.CompletedProcess([], 0, stdout=f"{self.values[service]}\n", stderr="")


def _stand_env(tmp_path, monkeypatch, stand):
    env_path = tmp_path / ".env"
    env_path.write_text(f"{QA_EXECUTOR_ENV}=codex\n", encoding="utf-8")
    stand.env_path = env_path
    monkeypatch.setattr(stand_run, "REPO", tmp_path)
    monkeypatch.setattr(stand_run, "_compose", stand)
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
