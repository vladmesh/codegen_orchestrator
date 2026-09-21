"""The stand deadline ledger's one derived budget."""

import pytest

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
