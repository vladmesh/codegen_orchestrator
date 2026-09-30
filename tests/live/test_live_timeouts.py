"""Every live test runs under a `pytest-timeout` bound, and a hang is reported, not killed.

Offline: the contract collects `tests/live` in a child pytest and reads back what
pytest-timeout itself resolves for each item; the hang regressions run a tiny
lifecycle in a child pytest with tiny bounds and stand_run's own flags.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from shared.stand_deadlines import (
    BRIEF_TEST_BOUNDS,
    LIVE_TEST_BOUNDS,
    NOOP_TEST_BOUNDS,
    ORDINARY_TEST_BOUNDS,
)

pytestmark = pytest.mark.needs_no_api_credential

LIVE_DIR = Path(__file__).resolve().parent
REPO = LIVE_DIR.parents[1]
FULL_PIPELINE_CLASS = "tests/live/test_full_pipeline.py::TestFullPipeline::"
BRIEF_CLASS = "tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline::"
PACKAGE_CLASS = (
    "tests/live/test_product_brief_package_pipeline.py::TestProductBriefPackagePipeline::"
)
# What `stand_run.run_pytest` passes after the target.
STAND_RUN_FLAGS = ("-x", "-q", "-s", "--tb=short")
CHILD_DEADLINE_SECONDS = 60
# The one module that cannot be collected at all: it imports
# `services/langgraph/src/consumers/_ci_gate.py`, which no longer exists, so it
# has no item to bound. Every other module under `tests/live` is collected.
UNCOLLECTABLE = "--ignore=tests/live/test_ci_prompt.py"

PROBE_PLUGIN = """
import json
import os

import pytest
import pytest_timeout


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    import live_timeouts

    rows = []
    for item in session.items:
        settings = pytest_timeout._get_item_settings(item)
        rows.append(
            {
                "nodeid": item.nodeid,
                "timeout": settings.timeout,
                "method": settings.method,
                "func_only": settings.func_only,
                "teardown": item.stash.get(live_timeouts.TEARDOWN_TIMEOUT_KEY, None),
            }
        )
    with open(os.environ["TIMEOUT_PROBE_OUT"], "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
"""


def _child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LIVE_WORKER_AGENT_TYPE", "LIVE_QA_AGENT_TYPE", "LIVE_LLM_QA"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(tmp_path), str(REPO), env.get("PYTHONPATH")))
    )
    env["LIVE_CONTOUR"] = "stand"
    env.update(extra)
    return env


def _collect(tmp_path: Path, **extra: str) -> dict[str, dict]:
    (tmp_path / "timeout_probe.py").write_text(PROBE_PLUGIN, encoding="utf-8")
    out = tmp_path / "items.json"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/live",
            UNCOLLECTABLE,
            "--collect-only",
            "-q",
            "-p",
            "timeout_probe",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        # Collection reaches the modules that call the API, so the credential
        # guard asks for a key; nothing is ever sent with it.
        env=_child_env(
            tmp_path, TIMEOUT_PROBE_OUT=str(out), INTERNAL_API_KEY="collect-only", **extra
        ),
        capture_output=True,
        text=True,
        timeout=CHILD_DEADLINE_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {row["nodeid"]: row for row in json.loads(out.read_text(encoding="utf-8"))}


def _members(items: dict[str, dict], prefix: str) -> list[dict]:
    members = [row for nodeid, row in items.items() if nodeid.startswith(prefix)]
    assert members, f"collected nothing under {prefix}"
    return members


@pytest.fixture(scope="module")
def collected(tmp_path_factory) -> dict[str, dict]:
    """`tests/live` as the scripted level-1 run collects it."""
    return _collect(tmp_path_factory.mktemp("collect"))


def test_every_collected_live_test_has_a_finite_signal_bound(collected):
    """No live test declares a timeout marker; every one still resolves to a finite bound."""
    items = collected

    assert len(items) > 100
    unbounded = {
        nodeid: row
        for nodeid, row in items.items()
        if not (
            isinstance(row["timeout"], (int, float))
            and row["timeout"] > 0
            and row["method"] == "signal"
            and row["func_only"] is False
            and isinstance(row["teardown"], int)
            and row["teardown"] > 0
        )
    }
    assert unbounded == {}


def test_the_item_that_sets_up_a_lifecycle_carries_its_waits(collected):
    items = collected

    level1 = _members(items, FULL_PIPELINE_CLASS)
    assert level1[0]["timeout"] == NOOP_TEST_BOUNDS.setup_item_seconds
    assert {row["timeout"] for row in level1[1:]} == {NOOP_TEST_BOUNDS.item_seconds}
    assert {row["teardown"] for row in level1} == {NOOP_TEST_BOUNDS.teardown_seconds}
    for prefix in (BRIEF_CLASS, PACKAGE_CLASS):
        brief = _members(items, prefix)
        assert brief[0]["timeout"] == BRIEF_TEST_BOUNDS.setup_item_seconds
        assert {row["timeout"] for row in brief[1:]} == {BRIEF_TEST_BOUNDS.item_seconds}
    ordinary = items["tests/live/test_health.py::test_api_health"]
    assert ordinary["timeout"] == ORDINARY_TEST_BOUNDS.item_seconds


def test_a_model_developer_gives_the_lifecycle_the_level_2_bound(tmp_path):
    items = _collect(tmp_path, LIVE_WORKER_AGENT_TYPE="codex")

    level1 = _members(items, FULL_PIPELINE_CLASS)
    assert level1[0]["timeout"] == LIVE_TEST_BOUNDS.setup_item_seconds
    assert {row["teardown"] for row in level1} == {LIVE_TEST_BOUNDS.teardown_seconds}


def test_a_deselected_first_item_moves_the_lifecycle_bound_to_the_next(collected, tmp_path):
    """With `-k`, whichever item runs first is the one that sets the fixture up."""
    first = _members(collected, FULL_PIPELINE_CLASS)[0]["nodeid"]
    name = first.rsplit("::", 1)[1]
    (tmp_path / "timeout_probe.py").write_text(PROBE_PLUGIN, encoding="utf-8")
    out = tmp_path / "selected.json"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/live/test_full_pipeline.py",
            "--collect-only",
            "-q",
            "-k",
            f"not {name}",
            "-p",
            "timeout_probe",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=_child_env(tmp_path, TIMEOUT_PROBE_OUT=str(out), INTERNAL_API_KEY="collect-only"),
        capture_output=True,
        text=True,
        timeout=CHILD_DEADLINE_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert first not in {row["nodeid"] for row in rows}
    assert rows[0]["timeout"] == NOOP_TEST_BOUNDS.setup_item_seconds


# ── A hang is a pytest failure ───────────────────────────────────────────
# The child's conftest is the live conftest's two hooks over `live_timeouts`,
# with the level-1 and ordinary bounds shrunk to seconds. Its test module is
# named like the real one, so the real resolution picks the lifecycle bound.

CHILD_CONFTEST = """
from pathlib import Path

import pytest

import live_timeouts
from shared.stand_deadlines import LiveTestBounds

live_timeouts.NOOP_TEST_BOUNDS = LiveTestBounds(
    setup_item_seconds=2, item_seconds=2, teardown_seconds=5, suite_cap_seconds=None
)
live_timeouts.ORDINARY_TEST_BOUNDS = live_timeouts.NOOP_TEST_BOUNDS


def pytest_collection_finish(session):
    live_timeouts.apply_bounds(session.items, root=Path(__file__).resolve().parent)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    live_timeouts.arm_teardown_bound(item)
    return (yield)
"""

HANGING_LIFECYCLE = """
import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="module")
MARKS = Path(os.environ["HANG_MARKS"])


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def pipeline():
    try:
        await asyncio.sleep(float(os.environ["SETUP_SECONDS"]))
        yield {}
    finally:
        # The cleanup awaits, like `cleanup_guard`'s does.
        await asyncio.sleep(float(os.environ["CLEANUP_SECONDS"]))
        MARKS.write_text("cleanup ran\\n", encoding="utf-8")


class TestFullPipeline:
    async def test_the_first(self, pipeline):
        assert not os.environ.get("FAIL_FIRST"), "the first assertion failed"

    async def test_the_second(self, pipeline):
        pass
"""

HANGING_TEST = """
import os
from pathlib import Path
import time

MARKS = Path(os.environ["HANG_MARKS"])


def test_hangs_in_its_call():
    try:
        time.sleep(60)
    finally:
        MARKS.write_text("finally ran\\n", encoding="utf-8")


def test_never_reached():
    pass
"""


def _run_child(tmp_path: Path, module: str, source: str, *args: str, **extra: str):
    (tmp_path / "conftest.py").write_text(CHILD_CONFTEST, encoding="utf-8")
    (tmp_path / module).write_text(source, encoding="utf-8")
    marks = tmp_path / "marks.txt"
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            module,
            *STAND_RUN_FLAGS,
            *args,
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "--rootdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=_child_env(
            tmp_path,
            PYTHONPATH=os.pathsep.join((str(LIVE_DIR), str(REPO))),
            HANG_MARKS=str(marks),
            **extra,
        ),
        capture_output=True,
        text=True,
        timeout=CHILD_DEADLINE_SECONDS,
        check=False,
    )
    return result, marks, time.monotonic() - started


def test_a_lifecycle_that_hangs_in_setup_fails_as_a_timeout_and_still_cleans_up(tmp_path):
    result, marks, elapsed = _run_child(
        tmp_path,
        "test_full_pipeline.py",
        HANGING_LIFECYCLE,
        SETUP_SECONDS="60",
        CLEANUP_SECONDS="0.2",
    )
    output = result.stdout + result.stderr

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert "Timeout (>2.0s) from pytest-timeout" in output
    assert "test_full_pipeline.py::TestFullPipeline::test_the_first" in output
    assert "stopping after 1 failures" in output
    assert "test_the_second" not in output
    assert marks.read_text(encoding="utf-8") == "cleanup ran\n"
    assert elapsed < 30


def test_a_test_that_hangs_in_its_call_fails_as_a_timeout_and_its_finally_runs(tmp_path):
    result, marks, elapsed = _run_child(tmp_path, "test_ordinary.py", HANGING_TEST)
    output = result.stdout + result.stderr

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert "Timeout (>2.0s) from pytest-timeout" in output
    assert "FAILED test_ordinary.py::test_hangs_in_its_call" in output
    assert "test_never_reached" not in output
    assert marks.read_text(encoding="utf-8") == "finally ran\n"
    assert elapsed < 30


def test_a_cleanup_that_hangs_after_a_failure_is_bounded_by_its_teardown_reserve(tmp_path):
    """pytest-timeout drops an item's timer once its setup or call fails.

    It cancels on `pytest_exception_interact`, which pytest calls for every
    failure, not only under `--pdb`. The teardown after a failed lifecycle item
    would then run unbounded; re-arming it is what reports a wedged cleanup as a
    teardown timeout. One selected item both sets the module fixture up and tears
    it down, as it does whenever `-k` selects a single lifecycle test.
    """
    result, marks, elapsed = _run_child(
        tmp_path,
        "test_full_pipeline.py",
        HANGING_LIFECYCLE,
        "-k",
        "test_the_first",
        SETUP_SECONDS="0.1",
        CLEANUP_SECONDS="60",
        FAIL_FIRST="1",
    )
    output = result.stdout + result.stderr

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert "the first assertion failed" in output
    assert "ERROR at teardown of TestFullPipeline.test_the_first" in output
    assert "Timeout (>5.0s) from pytest-timeout" in output
    assert not marks.exists()
    assert elapsed < 30


def test_a_healthy_lifecycle_inside_its_bounds_passes(tmp_path):
    result, marks, _elapsed = _run_child(
        tmp_path,
        "test_full_pipeline.py",
        HANGING_LIFECYCLE,
        SETUP_SECONDS="0.1",
        CLEANUP_SECONDS="0.1",
    )

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert marks.read_text(encoding="utf-8") == "cleanup ran\n"


def test_a_live_test_with_its_own_timeout_marker_is_refused(tmp_path):
    """The ledger is the one source of bounds; a second one would be unchecked."""
    result, _marks, _elapsed = _run_child(
        tmp_path,
        "test_ordinary.py",
        "import pytest\n\n\n@pytest.mark.timeout(5)\ndef test_own_bound():\n    pass\n",
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "test_ordinary.py::test_own_bound sets its own timeout" in result.stderr
