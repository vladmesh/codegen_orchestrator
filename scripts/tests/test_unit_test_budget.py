"""The host profile's per-test budget (scripts/unit_test_budget.py), run in-process."""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

import pytest

from scripts import unit_test_budget as budget_plugin

pytest_plugins = ["pytester"]

SAMPLE = textwrap.dedent(
    """
    import pytest

    def test_plain():
        pass

    @pytest.mark.slow(reason="waits for a real child")
    def test_slow():
        pass

    @pytest.mark.subprocess
    def test_spawns():
        pass

    def test_already_red():
        assert False
    """
)


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    pytester.makepyfile(test_sample=SAMPLE)
    return pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-p", "scripts.unit_test_budget", "-q", *args
    )


def test_a_test_over_the_budget_fails_unless_it_is_ci_only(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "--unit-test-budget=0")
    # The call passed; the budget fails its teardown, so pytest counts it as an error too.
    result.assert_outcomes(passed=3, failed=1, errors=1)
    result.stdout.fnmatch_lines(["*test_plain took *s (setup+call+teardown), over the 0s unit*"])
    assert "test_slow took" not in result.stdout.str()
    assert "test_already_red took" not in result.stdout.str()


def test_no_budget_option_changes_nothing(pytester: pytest.Pytester) -> None:
    _run(pytester).assert_outcomes(passed=3, failed=1)


def test_slow_and_subprocess_imply_ci_only(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "-m", "not ci_only", "--unit-test-budget=60")
    result.assert_outcomes(passed=1, failed=1, deselected=2)


def test_slow_without_a_reason_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_bare="import pytest\n\n@pytest.mark.slow\ndef test_x():\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "scripts.ci_only_markers", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_the_suite_cpu_file_holds_the_session_cpu(pytester: pytest.Pytester) -> None:
    out = pytester.path / "suite.json"
    _run(pytester, f"--suite-cpu-file={out}")
    assert json.loads(out.read_text())["cpu_seconds"] > 0


class _Clock:
    """A perf counter that moves only when a test says so: no real wait."""

    def __init__(self) -> None:
        self.now = 100.0

    def perf_counter(self) -> float:
        return self.now


class _Item:
    """The slice of a pytest item the budget reads."""

    def __init__(self, budget: float, *markers: str) -> None:
        self.nodeid = "test_sample.py::test_it"
        self.stash: dict = {}
        self.config = SimpleNamespace(getoption=lambda name: budget)
        self._markers = set(markers)

    def get_closest_marker(self, name: str):
        return name if name in self._markers else None


class _Outcome:
    def __init__(self, report) -> None:
        self.report = report

    def get_result(self):
        return self.report


def _report(when: str, duration: float):
    return SimpleNamespace(when=when, duration=duration, failed=False, outcome="passed")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(budget_plugin, "time", SimpleNamespace(perf_counter=fake.perf_counter))
    # No host load and no garbage collection in these runs.
    monkeypatch.setattr(budget_plugin._HostCost, "since_last_report", classmethod(lambda cls: 0.0))
    monkeypatch.setattr(budget_plugin._suite, "seconds", 0.0)
    return fake


def _phases(item: _Item, durations: dict[str, float], during=None) -> list:
    """Report setup, call and teardown as pytest would, running `during(phase)` first."""
    reports = []
    for when in ("setup", "call", "teardown"):
        if during:
            during(when)
        hook = budget_plugin.pytest_runtest_makereport(item, None)
        next(hook)
        report = _report(when, durations[when])
        with pytest.raises(StopIteration):
            hook.send(_Outcome(report))
        reports.append(report)
    return reports


def test_a_test_s_own_time_over_the_budget_fails_its_teardown(clock):
    reports = _phases(_Item(0.5), {"setup": 0.1, "call": 0.3, "teardown": 0.2})

    assert [r.outcome for r in reports] == ["passed", "passed", "failed"]
    assert "took 0.60s" in reports[-1].longrepr


def test_a_ci_only_family_test_is_exempt(clock):
    reports = _phases(_Item(0.5, "slow"), {"setup": 0.1, "call": 3.0, "teardown": 0.1})

    assert reports[-1].outcome == "passed"


def test_shared_fixture_setup_is_not_charged_to_the_first_test(clock):
    def session_fixture(when):
        if when != "setup":
            return
        hook = budget_plugin.pytest_fixture_setup(SimpleNamespace(scope="session"))
        next(hook)
        clock.now += 2.0  # the fixture's own work
        with pytest.raises(StopIteration):
            next(hook)

    reports = _phases(
        _Item(0.5), {"setup": 2.05, "call": 0.1, "teardown": 0.0}, during=session_fixture
    )

    assert reports[-1].outcome == "passed"


def test_a_first_import_is_not_charged_to_the_test_that_makes_it(clock):
    def heavy_import(*_args):
        clock.now += 1.5
        return "module"

    importer = budget_plugin._timed(heavy_import)

    def first_import(when):
        if when == "call":
            assert importer("heavy") == "module"

    reports = _phases(_Item(0.5), {"setup": 0.0, "call": 1.6, "teardown": 0.0}, during=first_import)

    assert reports[-1].outcome == "passed"
