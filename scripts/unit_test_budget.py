"""pytest plugin: the host profile's time budgets, per test and per suite.

`scripts/test-unit-local.sh` loads it (`-p scripts.unit_test_budget`) next to
`scripts.unit_test_timeout`, for every suite.

- `--unit-test-budget=SECONDS` (the host profile passes 0.5): a test whose
  setup + call + teardown takes longer fails in its teardown, naming the time it
  took. A test that legitimately needs longer carries `@pytest.mark.slow(reason=...)`
  or another ci_only-family marker (`scripts/ci_only_markers.py`), so it runs in CI
  only. The timings are pytest's own report durations, less what is not the
  test's own cost: setting up fixtures shared beyond one test (session, package,
  module or class scope) and importing modules for the first time, which land on
  whichever test happens to run first; garbage-collector pauses, which the suite's
  whole heap causes; and time the test thread spent runnable but waiting for a CPU
  (`/proc/thread-self/schedstat`), which is the load of a shared host. A real
  sleep or a wait on a child is still the test's. The 90 s `--timeout` stays the
  hang guard.
- `--suite-cpu-file=PATH`: at the end of the session the suite's CPU time, its own
  and that of every child it waited for (`getrusage`), is written to PATH as JSON.
  `python -m shared` reads these files to print CPU per suite.
"""

from __future__ import annotations

import builtins
import gc
import importlib
import json
from pathlib import Path
import resource
import threading
import time

import pytest

from scripts.ci_only_markers import CI_ONLY_FAMILY

_SPENT = pytest.StashKey[float]()
_FAILED = pytest.StashKey[bool]()
_PREVIOUS_IMPORTERS = pytest.StashKey[list[object]]()
_ADDED_GC_CALLBACK = pytest.StashKey[bool]()


class _SuiteCost(threading.local):
    """Seconds since the last report that belong to the suite, not to the running test."""

    seconds = 0.0
    importing = False


_suite = _SuiteCost()


class _HostCost:
    """Garbage-collector pauses and run-queue waits, as running totals in seconds."""

    gc_seconds = 0.0
    gc_started = 0.0
    seen = 0.0  # the total at the last report

    @classmethod
    def on_gc(cls, phase: str, _info: dict) -> None:
        if phase == "start":
            cls.gc_started = time.perf_counter()
        else:
            cls.gc_seconds += time.perf_counter() - cls.gc_started

    @classmethod
    def total(cls) -> float:
        return cls.gc_seconds + _runqueue_wait()

    @classmethod
    def since_last_report(cls) -> float:
        now = cls.total()
        spent, cls.seen = now - cls.seen, now
        return spent


def _runqueue_wait() -> float:
    """Seconds this thread was runnable but not running (Linux schedstat; else 0)."""
    try:
        with open("/proc/thread-self/schedstat") as stat:
            return int(stat.read().split()[1]) / 1e9
    except (OSError, IndexError, ValueError):
        return 0.0


def _timed(load):
    """`load` with its outermost call's time counted as the suite's."""

    def timed(*args, **kwargs):
        if _suite.importing:
            return load(*args, **kwargs)
        _suite.importing = True
        started = time.perf_counter()
        try:
            return load(*args, **kwargs)
        finally:
            _suite.seconds += time.perf_counter() - started
            _suite.importing = False

    return timed


# The `import` statement, and `importlib.import_module`, which `mock.patch` targets use.
_IMPORTERS = ((builtins, "__import__"), (importlib, "import_module"))


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("unit budget")
    group.addoption(
        "--unit-test-budget",
        type=float,
        default=None,
        help="fail a test whose setup+call+teardown exceeds this many seconds",
    )
    group.addoption(
        "--suite-cpu-file",
        default=None,
        help="write this pytest process's CPU seconds (self and children) to this JSON file",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("unit_test_budget") is None:
        return
    config.stash[_PREVIOUS_IMPORTERS] = [getattr(owner, name) for owner, name in _IMPORTERS]
    for owner, name in _IMPORTERS:
        setattr(owner, name, _timed(getattr(owner, name)))
    # A session nested in this one (pytester) shares the process: one callback for both.
    config.stash[_ADDED_GC_CALLBACK] = _HostCost.on_gc not in gc.callbacks
    if config.stash[_ADDED_GC_CALLBACK]:
        gc.callbacks.append(_HostCost.on_gc)
    _HostCost.since_last_report()


def pytest_unconfigure(config: pytest.Config) -> None:
    if _PREVIOUS_IMPORTERS not in config.stash:
        return
    for (owner, name), previous in zip(_IMPORTERS, config.stash[_PREVIOUS_IMPORTERS], strict=True):
        setattr(owner, name, previous)
    if config.stash[_ADDED_GC_CALLBACK]:
        gc.callbacks.remove(_HostCost.on_gc)


def _exempt(item: pytest.Item) -> bool:
    return any(item.get_closest_marker(name) for name in CI_ONLY_FAMILY)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef):
    if fixturedef.scope == "function":
        yield
        return
    before = _suite.seconds
    started = time.perf_counter()
    yield
    # The whole setup is the suite's, the imports it made included (counted once).
    _suite.seconds = before + time.perf_counter() - started


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    suite_cost, _suite.seconds = _suite.seconds, 0.0
    budget = item.config.getoption("unit_test_budget")
    if budget is None:
        return
    suite_cost += _HostCost.since_last_report()
    report: pytest.TestReport = outcome.get_result()
    spent = item.stash.get(_SPENT, 0.0) + max(report.duration - suite_cost, 0.0)
    item.stash[_SPENT] = spent
    failed = item.stash.get(_FAILED, False) or report.failed
    item.stash[_FAILED] = failed
    if report.when != "teardown" or failed or spent <= budget or _exempt(item):
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} took {spent:.2f}s (setup+call+teardown), over the {budget:g}s unit "
        "budget. Remove the real wait or process start, or mark it "
        '@pytest.mark.slow(reason="...") so it runs in CI only.'
    )


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = session.config.getoption("suite_cpu_file")
    if not path:
        return
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime
    Path(path).write_text(json.dumps({"cpu_seconds": round(cpu, 2)}))
