"""The host profile's per-test budget (scripts/unit_test_budget.py), run in-process."""

from __future__ import annotations

import json
import textwrap

import pytest

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


def test_shared_fixture_setup_is_not_charged_to_the_first_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_shared=textwrap.dedent(
            """
            import time

            import pytest

            @pytest.fixture(scope="session")
            def expensive():
                time.sleep(0.15)

            def test_first(expensive):
                pass

            def test_second(expensive):
                pass
            """
        )
    )
    result = pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-p", "scripts.unit_test_budget", "--unit-test-budget=0.1"
    )
    result.assert_outcomes(passed=2)


def test_a_first_import_is_not_charged_to_the_test_that_makes_it(
    pytester: pytest.Pytester,
) -> None:
    slow_import = "import time\n\ntime.sleep(0.15)\n"
    pytester.makepyfile(heavy_module=slow_import, heavy_patched=slow_import)
    pytester.makepyfile(
        test_lazy=textwrap.dedent(
            """
            from unittest import mock

            def test_imports_it():
                import heavy_module  # noqa: F401

            def test_patch_resolves_a_module():
                with mock.patch("heavy_patched.time"):
                    pass
            """
        )
    )
    pytester.syspathinsert()
    result = pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-p", "scripts.unit_test_budget", "--unit-test-budget=0.1"
    )
    result.assert_outcomes(passed=2)


def test_a_real_sleep_is_the_test_s_own_time(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sleeps="import time\n\ndef test_waits():\n    time.sleep(0.15)\n")
    result = pytester.runpytest_inprocess(
        "-p", "scripts.ci_only_markers", "-p", "scripts.unit_test_budget", "--unit-test-budget=0.1"
    )
    result.assert_outcomes(passed=1, errors=1)
