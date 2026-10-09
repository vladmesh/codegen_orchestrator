"""A selector that only reaches ci_only-family tests is refused, not passed as empty."""

from __future__ import annotations

import textwrap

import pytest

pytest_plugins = ["pytester"]

SAMPLE = textwrap.dedent(
    """
    import pytest

    def test_plain():
        pass

    @pytest.mark.docker
    def test_compose():
        pass

    @pytest.mark.slow(reason="waits for a real child")
    @pytest.mark.parametrize("n", [1, 2])
    def test_waits(n):
        pass
    """
)


def _run(pytester: pytest.Pytester, *selectors: str) -> pytest.RunResult:
    pytester.makepyfile(test_sample=SAMPLE)
    return pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-q", "-m", "not ci_only",
        "--refuse-ci-only-selection", *selectors,
    )  # fmt: skip


def test_a_ci_only_selector_names_its_marker(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "test_sample.py::test_compose")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*test_sample.py::test_compose selects only tests marked docker (ci_only family)*CI*"]
    )


def test_a_parametrized_selector_is_matched_whole(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "test_sample.py::test_waits")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*test_sample.py::test_waits selects only tests marked slow*"])


def test_one_ci_only_selector_among_runnable_ones_is_still_refused(
    pytester: pytest.Pytester,
) -> None:
    result = _run(pytester, "test_sample.py::test_plain", "test_sample.py::test_compose")
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_a_selector_with_runnable_tests_runs_them(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "test_sample.py")
    result.assert_outcomes(passed=1, deselected=3)


def test_without_the_option_an_empty_selection_stays_pytests_own(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(test_sample=SAMPLE)
    result = pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-q", "-m", "not ci_only", "test_sample.py::test_compose"
    )
    assert result.ret == pytest.ExitCode.NO_TESTS_COLLECTED
