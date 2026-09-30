"""The stand deadline ledger's derived budgets and the live tests' bounds."""

from pathlib import Path
import sys
import types

import pytest

from shared import stand_deadlines
from shared.stand_deadlines import scaffold_budget_seconds


def test_each_further_rendered_module_buys_its_own_make_setup_share():
    """A two-module product gets strictly more than a one-module one.

    The scaffolded product's `make setup` runs `uv sync --frozen` per service,
    so the budget grows with the module count rather than being one number for
    every product the stand scaffolds.
    """
    one = scaffold_budget_seconds(1)
    assert scaffold_budget_seconds(2) == 2 * one
    assert scaffold_budget_seconds(3) == 3 * one


def test_a_product_with_no_modules_is_refused_rather_than_given_a_budget():
    with pytest.raises(ValueError, match="at least one module"):
        scaffold_budget_seconds(0)


# ── Per-test bounds ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bounds", "waits", "reserve", "cap"),
    [
        pytest.param(
            stand_deadlines.NOOP_TEST_BOUNDS,
            stand_deadlines.noop_lifecycle_explicit_waits(),
            stand_deadlines.NOOP_TEARDOWN_RESERVE_SECONDS,
            stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS,
            id="mega-noop",
        ),
        pytest.param(
            stand_deadlines.LIVE_TEST_BOUNDS,
            stand_deadlines.live_lifecycle_explicit_waits(),
            stand_deadlines.LIVE_TEARDOWN_RESERVE_SECONDS,
            stand_deadlines.LIVE_SUITE_TIMEOUT_SECONDS,
            id="mega-live",
        ),
    ],
)
def test_a_lifecycle_hang_is_reported_by_pytest_before_the_runner_backstop(
    bounds, waits, reserve, cap
):
    """The setup item holds the lifecycle, the teardown its reserve, and both fit the cap."""
    assert bounds.setup_item_seconds >= waits
    assert bounds.teardown_seconds >= reserve
    assert bounds.max_item_seconds() + reserve < cap
    assert bounds.suite_cap_seconds == cap


def test_the_live_bounds_are_the_ledger_s_own_numbers():
    assert stand_deadlines.LIVE_TEST_TIMEOUT_METHOD == "signal"
    assert stand_deadlines.NOOP_TEST_BOUNDS == stand_deadlines.LiveTestBounds(
        setup_item_seconds=8440, item_seconds=1800, teardown_seconds=700, suite_cap_seconds=9300
    )
    assert stand_deadlines.LIVE_TEST_BOUNDS == stand_deadlines.LiveTestBounds(
        setup_item_seconds=14980, item_seconds=1800, teardown_seconds=700, suite_cap_seconds=15900
    )
    ordinary = stand_deadlines.ORDINARY_TEST_BOUNDS
    assert ordinary.max_item_seconds() + ordinary.teardown_seconds < (
        stand_deadlines.CUSTOM_TARGET_TIMEOUT_SECONDS
    )
    # The brief keeps its own clock: its bound never cuts the productive window.
    assert stand_deadlines.BRIEF_TEST_BOUNDS.setup_item_seconds >= max(
        stand_deadlines.MEGA_BRIEF_HARD_STOP_SECONDS,
        stand_deadlines.MEGA_BRIEF_PACKAGE_HARD_STOP_SECONDS,
    )


def _import_edited_ledger(old: str, new: str) -> None:
    source = Path(stand_deadlines.__file__).read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    module = types.ModuleType("edited_stand_deadlines")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source.replace(old, new), "edited_stand_deadlines", "exec"), module.__dict__)  # noqa: S102
    finally:
        del sys.modules[module.__name__]


def test_the_unedited_ledger_imports():
    _import_edited_ledger("LIVE_TEST_TIMEOUT_METHOD = ", "LIVE_TEST_TIMEOUT_METHOD = ")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        pytest.param(
            "    setup_item_seconds=noop_lifecycle_explicit_waits(),\n",
            "    setup_item_seconds=noop_lifecycle_explicit_waits() + 200,\n",
            "mega-noop item bound",
            id="noop-item-reaches-the-backstop",
        ),
        pytest.param(
            "    setup_item_seconds=live_lifecycle_explicit_waits(),\n",
            "    setup_item_seconds=live_lifecycle_explicit_waits() - 1,\n",
            "no longer covers the lifecycle",
            id="live-setup-item-short-of-its-waits",
        ),
        pytest.param(
            "    teardown_seconds=NOOP_TEARDOWN_RESERVE_SECONDS,\n",
            "    teardown_seconds=NOOP_TEARDOWN_RESERVE_SECONDS - 1,\n",
            "mega-noop teardown bound",
            id="noop-teardown-short-of-its-reserve",
        ),
        pytest.param(
            "    setup_item_seconds=LLM_ENGINEERING_TIMEOUT,\n",
            "    setup_item_seconds=CUSTOM_TARGET_TIMEOUT_SECONDS,\n",
            "ordinary live test item bound",
            id="ordinary-item-reaches-the-custom-target-backstop",
        ),
    ],
)
def test_an_edit_that_breaks_the_bound_ordering_fails_at_import(old, new, message):
    with pytest.raises(RuntimeError, match=message):
        _import_edited_ledger(old, new)
