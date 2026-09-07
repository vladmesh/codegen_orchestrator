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
