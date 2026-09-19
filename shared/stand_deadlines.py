"""One deadline ledger shared by the stand runner and its Product Brief fixtures."""

MEGA_BRIEF_PRODUCTIVE_SECONDS = 50 * 60
MEGA_BRIEF_HARD_STOP_SECONDS = 60 * 60

# The package variant of the brief buys one thing the digest variant does not:
# the engineering turn obtains the kit at this product's pin, builds the package
# wheel, installs it with `kit add` and regenerates the product contract before
# any of its own work starts. That install is minutes of real work on top of the
# same lifecycle, so the variant gets its own productive window rather than
# spending the digest ledger and dying inside it.
MEGA_BRIEF_PACKAGE_PRODUCTIVE_SECONDS = 65 * 60
MEGA_BRIEF_PACKAGE_HARD_STOP_SECONDS = 80 * 60

for _productive, _hard_stop in (
    (MEGA_BRIEF_PRODUCTIVE_SECONDS, MEGA_BRIEF_HARD_STOP_SECONDS),
    (MEGA_BRIEF_PACKAGE_PRODUCTIVE_SECONDS, MEGA_BRIEF_PACKAGE_HARD_STOP_SECONDS),
):
    if _hard_stop <= _productive:
        raise RuntimeError("a brief hard stop must leave time for cooperative cleanup")


# ── Scaffold ────────────────────────────────────────────────────────────────
# The harness's bound on one scaffold. It is not a flat number, because the work
# it bounds is not flat: `make setup` in the scaffolded product runs
# `uv sync --frozen` for the root and then for *each* rendered service, and ends
# with `framework.generate` and ruff over the whole tree. Adding a module adds a
# whole sync.
#
# What the numbers are made of, from stand-e2e run 35406260851 (`mega-noop`, SHA
# bd308334, 2026-09-18): the level-1 product is two modules (`backend,tg_bot`)
# since card 1308, its scaffolder logged `scaffold_make_setup_start` at 23:58:07
# — about ten seconds into the wait — and at 23:59:57 it was still inside
# `make setup`, with no error and no failure event, when the flat 120-second
# budget expired. So the fixed part of a scaffold (GitHub repo creation, copier
# copy, commit, push, the status write) is small and essentially the entire
# budget is `make setup`.
#
# One module is therefore worth the 120 seconds the one-module suites have
# always finished inside, and every further rendered service is worth another
# 120: it is one more `uv sync --frozen` plus its share of generate and ruff,
# the same order of work as the first. Two modules get 240 s — twice the budget
# that run proved insufficient — and every one-module suite keeps exactly the
# budget it had.
SCAFFOLD_FIRST_MODULE_SECONDS = 120
SCAFFOLD_ADDITIONAL_MODULE_SECONDS = 120


def scaffold_budget_seconds(module_count: int) -> int:
    """The scaffold budget for a product that renders `module_count` services."""
    if module_count < 1:
        raise ValueError(f"a scaffold renders at least one module, got {module_count}")
    return SCAFFOLD_FIRST_MODULE_SECONDS + (module_count - 1) * SCAFFOLD_ADDITIONAL_MODULE_SECONDS
