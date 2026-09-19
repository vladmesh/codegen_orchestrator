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


# ── The live harness's own waits ────────────────────────────────────────
# Every bound the level-1 lifecycle actually waits on. They live here, beside
# the ledger that sums them, because the ledger has to *be* the waits rather
# than a second copy of them: round 5 of card 1316 booked 420 s for the merged
# deploy Run while `tests/live` had been waiting `DEPLOY_RUN_TIMEOUT` — 1320 s —
# since the image-publication bound was folded into it, and a cap derived from
# that ledger read as checked while understating the lifecycle by 1800 s.
#
# The direction of the import is forced. `tests/live/` is not an installed
# package and it already imports `shared.stand_deadlines`
# (`pipeline_helpers.py`), so a ledger in `shared/` that imported the harness
# would be a cycle. The definitions therefore live here and the harness imports
# them; `tests/live/pipeline_helpers.py` keeps the names it always had.

ENGINEERING_TIMEOUT = 420  # 7 min (worker spawn + noop + CI)
DEPLOY_TIMEOUT = 420  # 7 min (deploy.yml + smoke test)
# Merged PR → pr_poller cycle → deploy run carrying the merged head SHA.
# The wait for a deploy Run to *appear*. It legitimately spans the project's own
# CI: no Run is created until the merged commit's images are observed published,
# which is what keeps DEPLOY_TIMEOUT above meaning "deploy.yml + smoke" instead
# of quietly absorbing somebody else's build. So it is the old 420 s of merge
# detection and Run creation plus the producer's full image bound
# (`image_publication.IMAGE_PUBLICATION_TIMEOUT_SECONDS`, 900 s), after which the
# story is refused and no Run can ever appear. Derived rather than measured on
# purpose: it is the ceiling the gate itself imposes, so it cannot be too small.
DEPLOY_RUN_TIMEOUT = 1320
# The deploy consumer writes the run result right after the app reports its
# status, so this only covers that last write on the initial lifecycle. A
# settings-seed follow-up goes directly from Run discovery to this wait, so its
# derived budgets also include the full deploy lifecycle.
DEPLOY_OUTCOME_TIMEOUT = 120
#: What the *second* story of a project waits for instead of an application
#: status. The application is already `running` from the first story's deploy and
#: stays terminal throughout a redeploy, so polling it would answer instantly and
#: prove nothing; the deploy Run's typed outcome is the fact. This wait therefore
#: has to cover the deploy itself as well as the settling `DEPLOY_OUTCOME_TIMEOUT`
#: covers, which is exactly the sum of the two the first story spends.
SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT = DEPLOY_TIMEOUT + DEPLOY_OUTCOME_TIMEOUT
# Deploy hands off to QA on the scheduler's next poll, then QA retries the health
# check while the service finishes coming up.
QA_RUN_TIMEOUT = 300
# Completion is emitted after QA by the supervisor, then the durable owner
# notification is delivered to PO. Undeploy runs over the same bounded deploy
# consumer path as a normal deployment, but has no GitHub workflow phase.
STORY_COMPLETION_TIMEOUT = 180
OWNER_NOTIFICATION_TIMEOUT = 180
UNDEPLOY_TIMEOUT = 300
#: How long a story is watched for the scheduler's `complete_stories` cycle to
#: aggregate its finished tasks into a PR. It was a bare `for _ in range(20)`
#: with a three-second sleep in two of the harness's engineering waits, which is
#: why the ledger's entry for it used to be an allowance nothing pointed at: a
#: number in the ledger that corresponds to no constant is a number nobody can
#: check. It is the wait now, and both loops are written from it.
STORY_AGGREGATION_POLL_INTERVAL = 3
STORY_AGGREGATION_TIMEOUT = 60

#: The public health probe's own shape, which is what its bound is made of: it
#: tries each path in turn, gives each request the client timeout, and sleeps
#: between attempts. Named here rather than left as function defaults for the
#: same reason as the aggregation wait — the ledger has to be able to point at
#: something.
HEALTH_PROBE_PATHS = ("/health", "/v1/health")
HEALTH_PROBE_ATTEMPTS = 5
HEALTH_PROBE_RETRY_DELAY_SECONDS = 5
HEALTH_PROBE_REQUEST_TIMEOUT_SECONDS = 30


def health_probe_budget_seconds() -> int:
    """The worst case of one public health probe: every path timing out, every time."""
    requests = HEALTH_PROBE_ATTEMPTS * len(HEALTH_PROBE_PATHS)
    sleeps = HEALTH_PROBE_ATTEMPTS - 1
    return (
        requests * HEALTH_PROBE_REQUEST_TIMEOUT_SECONDS + sleeps * HEALTH_PROBE_RETRY_DELAY_SECONDS
    )


# ── The level-1 (`mega-noop`) lifecycle ─────────────────────────────────
# The suite's pytest cap is not a duration estimate: it is the sum of the
# explicit waits the lifecycle can actually spend, plus a reserve for
# manifest-owned teardown and diagnostics. Every entry below is one of the
# constants above — the wait itself, not a transcription of it — so a timeout
# that moves takes the ledger and the cap with it.
#
# Since card 1316 the lifecycle runs **two stories on one project**. The second
# one is not a repetition of the first: it plans one task instead of two, and it
# waits for its deploy Run's typed outcome rather than for an ApplicationStatus
# that is already `running` from the first story's deploy and stays terminal
# throughout a redeploy.
#: The level-1 product's rendered services, which is what sizes its scaffold.
NOOP_MODULE_COUNT = 2

NOOP_FIRST_STORY_WAITS: tuple[tuple[str, int], ...] = (
    ("scaffold", scaffold_budget_seconds(NOOP_MODULE_COUNT)),
    ("two ordered noop engineering Tasks", 2 * ENGINEERING_TIMEOUT),
    ("Story aggregation after both Tasks are done", STORY_AGGREGATION_TIMEOUT),
    ("merged deploy Run, including the product's own image publication", DEPLOY_RUN_TIMEOUT),
    ("deploy", DEPLOY_TIMEOUT),
    ("typed deploy outcome", DEPLOY_OUTCOME_TIMEOUT),
    ("public health probe", health_probe_budget_seconds()),
    ("deterministic QA", QA_RUN_TIMEOUT),
    ("Story.completed", STORY_COMPLETION_TIMEOUT),
    ("durable PO completion notification", OWNER_NOTIFICATION_TIMEOUT),
    # `wait_service_deployment` spends the deploy-outcome bound: the record it
    # selects is written on the same path, right after the application reports.
    ("exact service-deployment record", DEPLOY_OUTCOME_TIMEOUT),
)

NOOP_SECOND_STORY_WAITS: tuple[tuple[str, int], ...] = (
    ("extension story: one noop engineering Task", ENGINEERING_TIMEOUT),
    (
        "extension story: Story aggregation after its Task is done",
        STORY_AGGREGATION_TIMEOUT,
    ),
    (
        "extension story: merged deploy Run, including image publication",
        DEPLOY_RUN_TIMEOUT,
    ),
    (
        "extension story: typed deploy outcome, covering the deploy itself",
        SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT,
    ),
    ("extension story: the application's own terminal status", DEPLOY_TIMEOUT),
    ("extension story: public health probe", health_probe_budget_seconds()),
    ("extension story: deterministic QA", QA_RUN_TIMEOUT),
    ("extension story: Story.completed", STORY_COMPLETION_TIMEOUT),
    ("extension story: durable PO completion notification", OWNER_NOTIFICATION_TIMEOUT),
)

NOOP_TEARDOWN_WAITS: tuple[tuple[str, int], ...] = (
    ("undeploy Run", UNDEPLOY_TIMEOUT),
    ("terminal application and port-allocation release", UNDEPLOY_TIMEOUT),
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


#: The `mega-noop` pytest cap: 155 minutes. Two stories' waits plus the reserve.
#:
#: It fits the one `e2e` job the workflow gives the whole run
#: (`.github/workflows/stand-e2e.yml`, `timeout-minutes: 360`): 45 m of
#: provisioning, 10 m of pre-provisioning reserve, 5 m preflight, 3 m readiness,
#: 3 m executor switch, this 155 m, 5 m sweep and an 8 m job reserve come to
#: 234 m.
NOOP_SUITE_TIMEOUT_SECONDS = 9300

if NOOP_SUITE_TIMEOUT_SECONDS < noop_lifecycle_explicit_waits() + NOOP_TEARDOWN_RESERVE_SECONDS:
    raise RuntimeError(
        "the mega-noop cap no longer covers its own lifecycle: "
        f"{noop_lifecycle_explicit_waits()}s of explicit waits and "
        f"{NOOP_TEARDOWN_RESERVE_SECONDS}s of teardown reserve need more than "
        f"{NOOP_SUITE_TIMEOUT_SECONDS}s"
    )
