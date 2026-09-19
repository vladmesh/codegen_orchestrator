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


# ── The level-1 (`mega-noop`) lifecycle ─────────────────────────────────
# The suite's pytest cap is not a duration estimate: it is the sum of the
# explicit waits the lifecycle can actually spend, plus a reserve for
# manifest-owned teardown and diagnostics. It lives here, as the waits
# themselves, because three places used to state it — the runner's constant, its
# test's transcription of the ledger and `tests/live/README.md` — and two of
# them had already drifted from the third when the scaffold bound became a
# function of the module count.
#
# Since card 1316 the lifecycle runs **two stories on one project**. The second
# one is not a repetition of the first: it plans one task instead of two, and it
# waits for its deploy Run's typed outcome rather than for an ApplicationStatus
# that is already `running` from the first story's deploy and stays terminal
# throughout a redeploy. Its waits are therefore listed on their own below, and
# the cap moved with them rather than the ledger being trimmed to fit.
#: The level-1 product's rendered services, which is what sizes its scaffold.
NOOP_MODULE_COUNT = 2

NOOP_FIRST_STORY_WAITS: tuple[tuple[str, int], ...] = (
    ("scaffold", scaffold_budget_seconds(NOOP_MODULE_COUNT)),
    ("two ordered noop engineering Tasks", 840),
    ("Story aggregation after both Tasks are done", 60),
    ("merged deploy Run", 420),
    ("deploy", 420),
    ("typed deploy outcome", 120),
    ("five-attempt public health probe (two 30s paths and four sleeps)", 320),
    ("deterministic QA", 300),
    ("Story.completed", 180),
    ("durable PO completion notification", 180),
    ("exact service-deployment record", 120),
)

NOOP_SECOND_STORY_WAITS: tuple[tuple[str, int], ...] = (
    ("extension story: one noop engineering Task", 420),
    ("extension story: Story aggregation after its Task is done", 60),
    ("extension story: merged deploy Run", 420),
    ("extension story: typed deploy outcome, covering the deploy itself", 540),
    ("extension story: the application's own terminal status", 420),
    ("extension story: five-attempt public health probe", 320),
    ("extension story: deterministic QA", 300),
    ("extension story: Story.completed", 180),
    ("extension story: durable PO completion notification", 180),
)

NOOP_TEARDOWN_WAITS: tuple[tuple[str, int], ...] = (
    ("undeploy Run", 300),
    ("terminal application and port-allocation release", 300),
)

NOOP_LIFECYCLE_WAITS: tuple[tuple[str, int], ...] = (
    *NOOP_FIRST_STORY_WAITS,
    *NOOP_SECOND_STORY_WAITS,
    *NOOP_TEARDOWN_WAITS,
)

#: What the cap leaves after every explicit wait: manifest-owned teardown of the
#: project, repository, registry, workspace and target host, plus the evidence
#: artifact the fixture writes before any of it.
NOOP_TEARDOWN_RESERVE_SECONDS = 700


def noop_lifecycle_explicit_waits() -> int:
    """Every wait the level-1 lifecycle can spend, summed."""
    return sum(seconds for _label, seconds in NOOP_LIFECYCLE_WAITS)


#: The `mega-noop` pytest cap: 125 minutes. Two stories' waits plus the reserve.
NOOP_SUITE_TIMEOUT_SECONDS = 7500

if NOOP_SUITE_TIMEOUT_SECONDS < noop_lifecycle_explicit_waits() + NOOP_TEARDOWN_RESERVE_SECONDS:
    raise RuntimeError(
        "the mega-noop cap no longer covers its own lifecycle: "
        f"{noop_lifecycle_explicit_waits()}s of explicit waits and "
        f"{NOOP_TEARDOWN_RESERVE_SECONDS}s of teardown reserve need more than "
        f"{NOOP_SUITE_TIMEOUT_SECONDS}s"
    )
