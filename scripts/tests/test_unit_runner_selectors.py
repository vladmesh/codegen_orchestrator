"""`test-unit-local.sh -- <selector>...` runs only the selected tests inside a profile."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.subprocess

ROOT = Path(__file__).resolve().parents[2]
RUNNABLE = "scripts/tests/test_ci_only_markers.py::test_a_selector_with_runnable_tests_runs_them"


def _runner(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "test-unit-local.sh"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_a_node_id_runs_alone_in_its_suite() -> None:
    result = _runner("--host", "--", RUNNABLE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "Passed: 1" in result.stdout


def test_a_ci_only_node_is_refused_naming_its_marker() -> None:
    selector = f"{Path(__file__).relative_to(ROOT)}::test_a_node_id_runs_alone_in_its_suite"
    result = _runner("--host", "--", selector)
    assert result.returncode != 0
    assert f"{selector} selects only tests marked subprocess (ci_only family)" in result.stdout


def test_a_suite_left_to_ci_is_refused_in_the_host_profile() -> None:
    result = _runner("--host", "--", "tests/live")
    assert result.returncode == 1
    assert "suite live-offline, which runs in CI" in result.stderr


def test_a_selector_outside_every_suite_is_refused() -> None:
    result = _runner("--host", "--", "services/api/tests/integration")
    assert result.returncode == 2
    assert "in no unit suite" in result.stderr


def test_an_option_is_not_a_selector() -> None:
    result = _runner("--host", "--", "-m", "docker")
    assert result.returncode == 2
